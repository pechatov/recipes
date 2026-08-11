import mimetypes
import re
import unicodedata
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.postgres.search import TrigramSimilarity
from django.db import connection, transaction
from django.db.models import Case, FloatField, IntegerField, Max, Q, Value, When
from django.db.models.functions import Coalesce, Greatest
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils._os import safe_join
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from .carting.pipeline import attempt_needs_cleanup
from .forms import ImportRecipeForm, IngredientFormSet, RecipeForm, SetupForm, StepFormSet
from .importing.extractors import detect_source_type
from .importing.normalizer import estimate_nutrition
from .models import CartAttempt, CartRun, Category, ImportJob, Recipe, StorePreference
from .services import build_shopping_items, get_store_preferences


SEARCH_CANDIDATE_LIMIT = 500
MAX_SEARCH_RANK_FRAGMENTS = 32
MAX_SEARCH_QUERY_LENGTH = 120
MAX_SEARCH_TOKENS = 8
SEARCH_FIELDS = (
    "title",
    "description",
    "created_by__username",
    "created_by__first_name",
    "created_by__last_name",
    "categories__name",
    "ingredients__name",
)
NUTRITION_FIELDS = (
    "calories_per_serving",
    "proteins_per_serving",
    "fats_per_serving",
    "carbohydrates_per_serving",
    "calories_per_100g",
    "proteins_per_100g",
    "fats_per_100g",
    "carbohydrates_per_100g",
)


def health(request):
    return JsonResponse({"status": "ok"})


def login_view(request):
    if not get_user_model().objects.exists():
        return redirect("setup-owner")
    return auth_views.LoginView.as_view(template_name="registration/login.html")(request)


def _normalize_recipe_search(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold().replace("ё", "е"))
    return " ".join(re.findall(r"[^\W_]+", value, re.UNICODE))


def _is_search_subsequence(needle: str, haystack: str) -> bool:
    iterator = iter(haystack)
    return all(character in iterator for character in needle)


def _search_edit_distance(left: str, right: str) -> int:
    """Return Damerau-Levenshtein distance for short normalized search words."""
    if left == right:
        return 0
    previous_previous = None
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1]
                    + (left_character != right_character),
                )
            )
            if (
                previous_previous is not None
                and left_index > 1
                and right_index > 1
                and left_character == right[right_index - 2]
                and left[left_index - 2] == right_character
            ):
                current[-1] = min(
                    current[-1], previous_previous[right_index - 2] + 1
                )
        previous_previous, previous = previous, current
    return previous[-1]


def _search_token_matches(token: str, search_text: str) -> bool:
    if token in search_text:
        return True
    if len(token) <= 2:
        return False
    words = search_text.split()
    if any(_is_search_subsequence(token, word) for word in words):
        return True
    maximum_distance = 1 if len(token) <= 5 else 2 if len(token) <= 8 else 3
    return any(
        abs(len(token) - len(word)) <= maximum_distance
        and _search_edit_distance(token, word) <= maximum_distance
        for word in words
    )


def _recipe_matches_fuzzy_query(recipe: Recipe, query: str) -> bool:
    author = recipe.created_by
    search_text = _normalize_recipe_search(
        " ".join(
            (
                recipe.title,
                recipe.description,
                author.get_full_name() if author else "",
                author.username if author else "",
                *(category.name for category in recipe.categories.all()),
                *(ingredient.name for ingredient in recipe.ingredients.all()),
            )
        )
    )
    return all(
        _search_token_matches(token, search_text)
        for token in _normalize_recipe_search(query).split()
    )


def _search_candidate_filter(query: str) -> Q:
    candidate_filter = Q()
    for token in _normalize_recipe_search(query).split()[:MAX_SEARCH_TOKENS]:
        # Character fallbacks keep the candidate set complete for supported
        # substitutions/transpositions; bigrams still make common queries
        # more selective when the database can use them.
        fragments = set(token)
        if len(token) >= 3:
            fragments.update(
                token[index:index + 2] for index in range(len(token) - 1)
            )
        token_filter = Q()
        for fragment in fragments:
            # SQLite's LIKE does not case-fold non-ASCII text. These variants
            # keep the portable development database useful; PostgreSQL's
            # ILIKE naturally collapses them.
            for variant in {fragment, fragment.capitalize(), fragment.upper()}:
                for field in SEARCH_FIELDS:
                    token_filter |= Q(**{f"{field}__icontains": variant})
        candidate_filter &= token_filter
    return candidate_filter


def _exact_search_filter(query: str) -> Q:
    exact_filter = Q()
    for token in _normalize_recipe_search(query).split()[:MAX_SEARCH_TOKENS]:
        token_filter = Q()
        for variant in {token, token.capitalize(), token.upper()}:
            for field in SEARCH_FIELDS:
                token_filter |= Q(**{f"{field}__icontains": variant})
        exact_filter &= token_filter
    return exact_filter


def _ranked_fuzzy_candidates(recipes, query: str):
    candidates = recipes.filter(_search_candidate_filter(query)).distinct()
    if connection.vendor != "postgresql":
        fragments = []
        for token in _normalize_recipe_search(query).split()[:MAX_SEARCH_TOKENS]:
            fragments.extend(token)
            fragments.extend(
                token[index:index + 2] for index in range(max(0, len(token) - 1))
            )
        fragments = list(dict.fromkeys(fragments))[:MAX_SEARCH_RANK_FRAGMENTS]
        score_parts = []
        for fragment in fragments:
            fragment_filter = Q()
            for variant in {fragment, fragment.capitalize(), fragment.upper()}:
                for field in SEARCH_FIELDS:
                    fragment_filter |= Q(**{f"{field}__icontains": variant})
            score_parts.append(
                Max(
                    Case(
                        When(fragment_filter, then=Value(1)),
                        default=Value(0),
                        output_field=IntegerField(),
                    )
                )
            )
        score = sum(score_parts, Value(0, output_field=IntegerField()))
        return candidates.annotate(search_similarity=score).order_by(
            "-search_similarity", "-updated_at"
        )[:SEARCH_CANDIDATE_LIMIT]
    zero = Value(0.0, output_field=FloatField())
    similarities = [
        Coalesce(Max(TrigramSimilarity(field, query)), zero)
        for field in SEARCH_FIELDS
    ]
    return candidates.annotate(
        search_similarity=Greatest(*similarities)
    ).order_by("-search_similarity", "-updated_at")[:SEARCH_CANDIDATE_LIMIT]


@require_http_methods(["GET", "POST"])
def setup_owner(request):
    if get_user_model().objects.exists():
        return redirect("recipe-list" if request.user.is_authenticated else "login")

    form = SetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save(commit=False)
        user.is_staff = True
        user.is_superuser = True
        user.save()
        login(request, user)
        messages.success(request, "Домашняя книга рецептов готова.")
        return redirect("recipe-list")

    return render(request, "recipes/setup.html", {"form": form})


@login_required
def recipe_list(request):
    query = request.GET.get("q", "").strip()
    normalized_tokens = _normalize_recipe_search(query).split()
    if (
        len(query) > MAX_SEARCH_QUERY_LENGTH
        or len(normalized_tokens) > MAX_SEARCH_TOKENS
        or (query and not normalized_tokens)
    ):
        return JsonResponse(
            {"error": "Поисковый запрос некорректный или слишком длинный."},
            status=400,
        )
    selected_category = request.GET.get("category", "").strip()
    selected_author = request.GET.get("author", "").strip()
    recipes = (
        Recipe.objects.filter(status=Recipe.Status.PUBLISHED)
        .select_related("created_by")
        .prefetch_related("ingredients", "categories")
    )
    if selected_category:
        recipes = recipes.filter(categories__slug=selected_category)
    if selected_author.isdigit():
        recipes = recipes.filter(created_by_id=selected_author)
    if query:
        exact_matches = list(recipes.filter(_exact_search_filter(query)).distinct())
        fuzzy_candidates = _ranked_fuzzy_candidates(recipes, query)
        candidate_recipes = exact_matches + list(fuzzy_candidates)
        seen_recipe_ids = set()
        recipes = []
        for recipe in candidate_recipes:
            if recipe.pk in seen_recipe_ids:
                continue
            seen_recipe_ids.add(recipe.pk)
            if _recipe_matches_fuzzy_query(recipe, query):
                recipes.append(recipe)
    return render(
        request,
        "recipes/recipe_list.html",
        {
            "recipes": recipes,
            "query": query,
            "categories": Category.objects.all(),
            "selected_category": selected_category,
            "authors": get_user_model()
            .objects.filter(created_recipes__status=Recipe.Status.PUBLISHED)
            .distinct()
            .order_by("username"),
            "selected_author": selected_author,
        },
    )


@login_required
def draft_list(request):
    recipes = Recipe.objects.filter(status=Recipe.Status.DRAFT).prefetch_related(
        "ingredients", "categories"
    )
    jobs = ImportJob.objects.exclude(status=ImportJob.Status.COMPLETED)[:20]
    return render(request, "recipes/draft_list.html", {"recipes": recipes, "jobs": jobs})


@login_required
def recipe_detail(request, slug):
    recipe = get_object_or_404(
        Recipe.objects.select_related("created_by").prefetch_related(
            "ingredients", "steps", "import_jobs"
        ),
        slug=slug,
    )
    all_ingredients = list(recipe.ingredients.all())
    if recipe.calories_estimated or (
        recipe.calories_per_serving is None and recipe.calories_per_100g is None
    ):
        _fill_missing_recipe_calories(recipe, all_ingredients, save=False)
    ingredients = [ingredient for ingredient in all_ingredients if not ingredient.is_water]
    import_job = getattr(recipe, "import_job", None) or next(
        iter(recipe.import_jobs.all()), None
    )
    return render(
        request,
        "recipes/recipe_detail.html",
        {
            "recipe": recipe,
            "main_ingredients": [item for item in ingredients if not item.is_pantry],
            "pantry_ingredients": [item for item in ingredients if item.is_pantry],
            "source_import_job": import_job,
        },
    )


def _fill_missing_recipe_calories(
    recipe,
    ingredients=None,
    *,
    save: bool,
    overwrite: bool = False,
    preserve_fields: set[str] | None = None,
) -> None:
    preserve_fields = preserve_fields or set()
    if (
        not overwrite
        and all(getattr(recipe, field) is not None for field in NUTRITION_FIELDS)
    ):
        return
    ingredients = list(ingredients if ingredients is not None else recipe.ingredients.all())
    nutrition = estimate_nutrition(
        [
            {
                "name": ingredient.name,
                "quantity": (
                    str(ingredient.quantity) if ingredient.quantity is not None else None
                ),
                "unit": ingredient.unit,
            }
            for ingredient in ingredients
        ],
        recipe.servings,
    )
    changed = []
    for field, value in nutrition.items():
        if field in preserve_fields:
            continue
        if overwrite or getattr(recipe, field) is None:
            setattr(recipe, field, value)
            changed.append(field)
    if save and changed:
        recipe.save(update_fields=changed + ["updated_at"])


def _recipe_form_context(request, instance=None):
    recipe = instance or Recipe()
    form = RecipeForm(request.POST or None, request.FILES or None, instance=recipe)
    ingredient_formset = IngredientFormSet(
        request.POST or None,
        request.FILES or None,
        instance=recipe,
        prefix="ingredients",
    )
    step_formset = StepFormSet(
        request.POST or None,
        request.FILES or None,
        instance=recipe,
        prefix="steps",
    )
    return recipe, form, ingredient_formset, step_formset


@login_required
@require_http_methods(["GET", "POST"])
def recipe_create(request):
    recipe, form, ingredient_formset, step_formset = _recipe_form_context(request)
    if request.method == "POST" and form.is_valid() and ingredient_formset.is_valid() and step_formset.is_valid():
        with transaction.atomic():
            recipe = form.save(commit=False)
            recipe.created_by = request.user
            recipe.save()
            form.save_m2m()
            ingredient_formset.instance = recipe
            ingredient_formset.save()
            step_formset.instance = recipe
            step_formset.save()
            manual_calories = any(
                form.cleaned_data[field] is not None
                for field in NUTRITION_FIELDS
            )
            if not manual_calories:
                _fill_missing_recipe_calories(recipe, save=True)
            recipe.calories_estimated = not manual_calories
            recipe.save(update_fields=["calories_estimated", "updated_at"])
        messages.success(request, "Рецепт добавлен.")
        return redirect(recipe)

    return render(
        request,
        "recipes/recipe_form.html",
        {
            "recipe": recipe,
            "form": form,
            "ingredient_formset": ingredient_formset,
            "step_formset": step_formset,
            "is_create": True,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def recipe_update(request, slug):
    instance = get_object_or_404(Recipe, slug=slug)
    calories_were_estimated = instance.calories_estimated
    recipe, form, ingredient_formset, step_formset = _recipe_form_context(request, instance)
    if request.method == "POST" and form.is_valid() and ingredient_formset.is_valid() and step_formset.is_valid():
        with transaction.atomic():
            recipe = form.save()
            ingredient_formset.save()
            step_formset.save()
            calorie_fields = set(NUTRITION_FIELDS)
            manual_calorie_change = bool(calorie_fields.intersection(form.changed_data))
            ingredient_energy_fields = {"name", "quantity", "unit", "DELETE"}
            ingredients_changed = any(
                ingredient_energy_fields.intersection(ingredient_form.changed_data)
                for ingredient_form in ingredient_formset.forms
            )
            recalculate = "servings" in form.changed_data or ingredients_changed
            if manual_calorie_change:
                recipe.calories_estimated = False
                recipe.save(update_fields=["calories_estimated", "updated_at"])
            elif recalculate and (
                calories_were_estimated
                or (
                    all(getattr(recipe, field) is None for field in NUTRITION_FIELDS)
                )
            ):
                _fill_missing_recipe_calories(recipe, save=True, overwrite=True)
                recipe.calories_estimated = True
                recipe.save(update_fields=["calories_estimated", "updated_at"])
        messages.success(request, "Изменения сохранены.")
        return redirect(recipe)

    return render(
        request,
        "recipes/recipe_form.html",
        {
            "recipe": recipe,
            "form": form,
            "ingredient_formset": ingredient_formset,
            "step_formset": step_formset,
            "is_create": False,
        },
    )


@login_required
@require_POST
def recipe_publish(request, slug):
    recipe = get_object_or_404(Recipe, slug=slug, status=Recipe.Status.DRAFT)
    recipe.status = Recipe.Status.PUBLISHED
    recipe.save(update_fields=["status", "updated_at"])
    messages.success(request, "Рецепт опубликован и появился в общей книге.")
    return redirect(recipe)


@login_required
@require_http_methods(["GET", "POST"])
def import_create(request):
    form = ImportRecipeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        job = form.save(commit=False)
        job.source_type = detect_source_type(job.source_url)
        job.requested_by = request.user
        job.save()
        messages.success(request, "Ссылка добавлена в очередь. Можно закрыть эту страницу.")
        return redirect("import-detail", pk=job.pk)
    return render(request, "recipes/import_form.html", {"form": form})


@login_required
def import_detail(request, pk):
    job = get_object_or_404(
        ImportJob.objects.select_related("recipe").prefetch_related("recipes"),
        pk=pk,
        requested_by=request.user,
    )
    return render(request, "recipes/import_detail.html", {"job": job})


@login_required
@require_POST
def import_retry(request, pk):
    job = get_object_or_404(
        ImportJob,
        pk=pk,
        requested_by=request.user,
        status=ImportJob.Status.FAILED,
    )
    job.status = ImportJob.Status.PENDING
    job.error = ""
    job.started_at = None
    job.finished_at = None
    job.save(update_fields=["status", "error", "started_at", "finished_at"])
    messages.success(request, "Импорт снова поставлен в очередь.")
    return redirect("import-detail", pk=job.pk)


@login_required
@require_POST
def import_reprocess(request, pk):
    job = get_object_or_404(
        ImportJob.objects.select_related("recipe").prefetch_related("recipes"),
        pk=pk,
        requested_by=request.user,
        status=ImportJob.Status.COMPLETED,
    )
    has_published_recipe = (
        bool(job.recipe_id and job.recipe.status == Recipe.Status.PUBLISHED)
        or job.recipes.filter(status=Recipe.Status.PUBLISHED).exists()
    )
    if has_published_recipe:
        messages.error(
            request,
            "Повторная обработка недоступна: один из рецептов уже опубликован.",
        )
        return redirect("import-detail", pk=job.pk)
    drafts = list(job.recipes.filter(status=Recipe.Status.DRAFT))
    if job.recipe and job.recipe.status == Recipe.Status.DRAFT and job.recipe not in drafts:
        drafts.append(job.recipe)
    if not drafts:
        messages.info(request, "У этого импорта не осталось черновиков для обновления.")
        return redirect("import-detail", pk=job.pk)
    job.status = ImportJob.Status.PENDING
    job.error = ""
    job.started_at = None
    job.finished_at = None
    job.save(update_fields=["status", "error", "started_at", "finished_at"])
    messages.success(request, "Черновик поставлен на повторную обработку.")
    return redirect("import-detail", pk=job.pk)


@login_required
def task_list(request):
    import_jobs = (
        ImportJob.objects.filter(requested_by=request.user)
        .select_related("recipe")
        .prefetch_related("recipes")[:50]
    )
    cart_runs = CartRun.objects.filter(requested_by=request.user).select_related(
        "recipe", "selected_attempt"
    )[:50]
    return render(
        request,
        "recipes/task_list.html",
        {"import_jobs": import_jobs, "cart_runs": cart_runs},
    )


@login_required
@require_http_methods(["GET", "POST"])
def recipe_delete(request, slug):
    recipe = get_object_or_404(Recipe, slug=slug)
    if request.method == "POST":
        recipe.delete()
        messages.success(request, "Рецепт удалён.")
        return redirect("recipe-list")
    return render(request, "recipes/recipe_confirm_delete.html", {"recipe": recipe})


@login_required
def shopping_list(request, slug):
    recipe = get_object_or_404(Recipe.objects.prefetch_related("ingredients"), slug=slug)
    try:
        servings = int(request.GET.get("servings", recipe.servings))
    except (TypeError, ValueError):
        servings = recipe.servings
    servings = max(1, min(servings, 100))
    preferences = get_store_preferences(request.user)
    primary_store = next((item for item in preferences if item.enabled), None)
    items = build_shopping_items(
        recipe, servings, primary_store.store if primary_store else None
    )
    latest_cart_run = (
        CartRun.objects.filter(recipe=recipe, requested_by=request.user)
        .select_related("selected_attempt")
        .first()
    )
    return render(
        request,
        "recipes/shopping_list.html",
        {
            "recipe": recipe,
            "servings": servings,
            "items": items,
            "has_pantry_items": any(item.ingredient.is_pantry for item in items),
            "store_preferences": preferences,
            "primary_store": primary_store,
            "preferences_return_url": request.get_full_path(),
            "latest_cart_run": latest_cart_run,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def store_preferences(request):
    preferences = get_store_preferences(request.user)
    if request.method == "POST":
        posted_order = request.POST.getlist("store_order")
        valid_stores = {preference.store for preference in preferences}
        ordered_stores = [
            store for store in posted_order if store in valid_stores
        ]
        ordered_stores.extend(
            preference.store
            for preference in preferences
            if preference.store not in ordered_stores
        )
        position_by_store = {
            store: position for position, store in enumerate(ordered_stores)
        }
        updates = []
        for preference in preferences:
            if posted_order:
                preference.position = position_by_store[preference.store]
            else:
                try:
                    position = int(request.POST.get(f"position_{preference.store}", preference.position))
                except (TypeError, ValueError):
                    position = preference.position
                preference.position = max(0, min(position, 99))
            preference.enabled = f"enabled_{preference.store}" in request.POST
            updates.append(preference)
        StorePreference.objects.bulk_update(updates, ["position", "enabled"])
        messages.success(request, "Приоритет магазинов сохранён.")
        next_url = request.POST.get("next", "")
        if next_url and url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return redirect(next_url)
        return redirect("store-preferences")
    return render(request, "recipes/store_preferences.html", {"preferences": preferences})


@login_required
@require_POST
def cart_start(request, slug):
    recipe = get_object_or_404(Recipe.objects.prefetch_related("ingredients"), slug=slug)
    try:
        servings = max(1, min(int(request.POST.get("servings", recipe.servings)), 100))
    except (TypeError, ValueError):
        servings = recipe.servings

    selected_ids = set(request.POST.getlist("ingredients"))
    items = [
        item
        for item in build_shopping_items(recipe, servings)
        if str(item.ingredient.pk) in selected_ids
    ]
    if not items:
        messages.error(request, "Выберите хотя бы один ингредиент.")
        return redirect(f"{reverse('shopping-list', args=[recipe.slug])}?servings={servings}")

    active = CartRun.objects.filter(
        requested_by=request.user,
    ).filter(
        Q(
            status__in=[
                CartRun.Status.PENDING,
                CartRun.Status.PROCESSING,
                CartRun.Status.CLEANUP_PENDING,
                CartRun.Status.CLEANING,
                CartRun.Status.MANUAL_CHECK,
            ]
        )
        | Q(cleanup_requested_at__isnull=False, cleaned_at__isnull=True)
    ).first()
    if active:
        messages.info(request, "Сначала дождитесь уже запущенной сборки корзины.")
        return redirect("cart-detail", pk=active.pk)

    priority = [item.store for item in get_store_preferences(request.user) if item.enabled]
    if not priority:
        messages.error(request, "Включите хотя бы один магазин.")
        return redirect(
            f"{reverse('shopping-list', args=[recipe.slug])}?servings={servings}"
        )

    snapshot = [
        {
            "name": item.ingredient.name,
            "section": item.ingredient.section,
            "quantity": item.display_quantity,
            "unit": item.ingredient.unit,
            "search_query": item.ingredient.effective_search_query,
            "optional": item.ingredient.optional,
            "is_pantry": item.ingredient.is_pantry,
        }
        for item in items
    ]
    run = CartRun.objects.create(
        recipe=recipe,
        requested_by=request.user,
        servings=servings,
        store_priority=priority,
        ingredient_snapshot=snapshot,
    )
    messages.success(request, "Сборка запущена. Магазины будут проверены по приоритету.")
    return redirect("cart-detail", pk=run.pk)


@login_required
def cart_detail(request, pk):
    run = get_object_or_404(
        CartRun.objects.select_related("recipe", "selected_attempt").prefetch_related(
            "attempts__matches"
        ),
        pk=pk,
        requested_by=request.user,
    )
    attempts = list(run.attempts.all())
    status_attempt = run.selected_attempt or (attempts[-1] if attempts else None)
    matches = {
        match.ingredient_name.casefold(): match
        for match in (status_attempt.matches.all() if status_attempt else [])
    }
    quality_labels = {
        "exact": "Найдено полное совпадение",
        "substitute": "Найдена альтернатива",
        "missing": "Ничего не найдено",
    }
    item_statuses = []
    for item in run.ingredient_snapshot:
        name = str(item.get("name", "")).strip()
        match = matches.get(name.casefold())
        quality = match.quality if match else "queued"
        item_statuses.append(
            {
                "name": name,
                "quantity": " ".join(
                    part
                    for part in (str(item.get("quantity", "")), str(item.get("unit", "")))
                    if part
                ),
                "quality": quality,
                "label": quality_labels.get(quality, "В очереди"),
                "match": match,
            }
        )
    return render(
        request,
        "recipes/cart_detail.html",
        {"run": run, "cart_item_statuses": item_statuses},
    )


@login_required
@require_POST
def cart_continue(request, pk):
    run = get_object_or_404(CartRun, pk=pk, requested_by=request.user)
    if run.is_active:
        return redirect("cart-detail", pk=run.pk)
    if run.status not in {CartRun.Status.COMPLETED, CartRun.Status.REVIEW}:
        messages.info(request, "Эту корзину нельзя продолжить.")
        return redirect("cart-detail", pk=run.pk)
    if run.next_store_index >= len(run.store_priority):
        messages.info(request, "Все включённые магазины уже проверены.")
        return redirect("cart-detail", pk=run.pk)
    now = timezone.now()
    if attempt_needs_cleanup(run.selected_attempt):
        run.status = CartRun.Status.CLEANUP_PENDING
        run.cleanup_requested_at = now
        run.confirmation_deadline = None
        run.save(
            update_fields=[
                "status",
                "cleanup_requested_at",
                "confirmation_deadline",
            ]
        )
    else:
        run.status = CartRun.Status.CANCELLED
        run.cleaned_at = now
        run.confirmation_deadline = None
        run.save(update_fields=["status", "cleaned_at", "confirmation_deadline"])

    next_run = CartRun.objects.create(
        recipe=run.recipe,
        requested_by=run.requested_by,
        servings=run.servings,
        store_priority=run.store_priority,
        ingredient_snapshot=run.ingredient_snapshot,
        next_store_index=run.next_store_index,
    )
    messages.success(
        request,
        "Текущие добавления будут очищены, затем проверим следующий магазин.",
    )
    return redirect("cart-detail", pk=next_run.pk)


@login_required
@require_POST
def cart_confirm(request, pk):
    run = get_object_or_404(
        CartRun,
        pk=pk,
        requested_by=request.user,
        status__in=[CartRun.Status.COMPLETED, CartRun.Status.REVIEW],
    )
    if not run.selected_attempt or not run.selected_attempt.cart_url:
        messages.error(request, "Нет сохранённой корзины для подтверждения.")
        return redirect("cart-detail", pk=run.pk)
    run.status = CartRun.Status.CONFIRMED
    run.confirmed_at = timezone.now()
    run.confirmation_deadline = None
    run.error = ""
    run.save(
        update_fields=["status", "confirmed_at", "confirmation_deadline", "error"]
    )
    messages.success(request, "Корзина подтверждена и не будет очищена автоматически.")
    return redirect("cart-detail", pk=run.pk)


@login_required
@require_POST
def cart_cancel(request, pk):
    run = get_object_or_404(
        CartRun,
        pk=pk,
        requested_by=request.user,
        status__in=[CartRun.Status.COMPLETED, CartRun.Status.REVIEW],
    )
    now = timezone.now()
    run.confirmation_deadline = None
    if attempt_needs_cleanup(run.selected_attempt):
        run.status = CartRun.Status.CLEANUP_PENDING
        run.cleanup_requested_at = now
        fields = ["status", "cleanup_requested_at", "confirmation_deadline"]
        messages.info(request, "Добавленные этой попыткой товары поставлены на очистку.")
    else:
        run.status = CartRun.Status.CANCELLED
        run.cleaned_at = now
        fields = ["status", "cleaned_at", "confirmation_deadline"]
        messages.info(request, "Сборка отменена; очищать корзину не потребовалось.")
    run.save(update_fields=fields)
    return redirect("cart-detail", pk=run.pk)


@login_required
@require_POST
def cart_manual_resolved(request, pk):
    with transaction.atomic():
        run = get_object_or_404(
            CartRun.objects.select_for_update().select_related("selected_attempt"),
            pk=pk,
            requested_by=request.user,
            status=CartRun.Status.MANUAL_CHECK,
        )
        attempt = run.selected_attempt
        if not attempt:
            messages.error(request, "Не найдена попытка, требующая проверки.")
            return redirect("cart-detail", pk=run.pk)

        now = timezone.now()
        result = dict(attempt.result or {})
        result["mutation_unknown"] = False
        result["manual_check_resolved_at"] = now.isoformat()
        result["cart_cleared"] = True
        attempt.result = result
        attempt.cart_url = ""
        attempt.save(update_fields=["result", "cart_url"])

        run.error = ""
        run.confirmation_deadline = None
        if run.cleanup_requested_at:
            run.status = CartRun.Status.CANCELLED
            run.cleaned_at = now
            run.finished_at = now
            fields = [
                "status",
                "cleaned_at",
                "finished_at",
                "confirmation_deadline",
                "error",
            ]
            message = "Ручная проверка сохранена; сборка отменена."
        else:
            run.status = CartRun.Status.PENDING
            run.selected_attempt = None
            run.started_at = None
            run.finished_at = None
            fields = [
                "status",
                "selected_attempt",
                "started_at",
                "finished_at",
                "confirmation_deadline",
                "error",
            ]
            message = "Ручная проверка сохранена; сборка снова поставлена в очередь."
        run.save(update_fields=fields)

    messages.success(request, message)
    return redirect("cart-detail", pk=run.pk)


@login_required
@require_POST
def cart_retry(request, pk):
    run = get_object_or_404(
        CartRun,
        pk=pk,
        requested_by=request.user,
        status__in=[CartRun.Status.LOGIN_REQUIRED, CartRun.Status.FAILED],
    )
    if run.cleanup_requested_at and not run.cleaned_at:
        run.status = CartRun.Status.CLEANUP_PENDING
        run.finished_at = None
        run.error = ""
        run.save(update_fields=["status", "finished_at", "error"])
        messages.success(request, "Очистка снова поставлена в очередь.")
        return redirect("cart-detail", pk=run.pk)
    if run.status == CartRun.Status.FAILED:
        blocked_stores = set(
            run.attempts.filter(status=CartAttempt.Status.BLOCKED).values_list(
                "store", flat=True
            )
        )
        for index, store in enumerate(run.store_priority):
            if store in blocked_stores:
                run.next_store_index = index
                break
    run.status = CartRun.Status.PENDING
    run.finished_at = None
    run.error = ""
    run.save(update_fields=["status", "next_store_index", "finished_at", "error"])
    messages.success(request, "Попытка снова поставлена в очередь.")
    return redirect("cart-detail", pk=run.pk)


@login_required
def media_file(request, path):
    try:
        resolved = Path(safe_join(settings.MEDIA_ROOT, path))
    except ValueError as error:
        raise Http404 from error
    if not resolved.is_file():
        raise Http404
    content_type, _ = mimetypes.guess_type(resolved.name)
    return FileResponse(resolved.open("rb"), content_type=content_type or "application/octet-stream")

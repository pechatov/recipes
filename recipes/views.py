import mimetypes
import re
import secrets
import unicodedata
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.postgres.search import TrigramWordSimilarity
from django.db import IntegrityError, connection, transaction
from django.db.models import Case, FloatField, IntegerField, Max, Q, Value, When
from django.db.models.functions import Coalesce, Greatest
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils._os import safe_join
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from .carting.browser_login import (
    BrowserLoginError,
    BrowserLoginSessionNotFound,
    is_configured as browser_login_is_configured,
    issue_access as issue_browser_login_access,
    start_session as start_browser_login_session,
    stop_session as stop_browser_login_session,
)
from .carting.client import cart_browser_session_key
from .carting.coordination import (
    BROWSER_BLOCKING_STATUSES,
    reconcile_expired_browser_login_sessions,
    reconcile_missing_browser_login_session,
)
from .carting.pipeline import attempt_needs_cleanup
from .forms import (
    ImportRecipeForm,
    IngredientFormSet,
    RecipeForm,
    RecipeRefinementForm,
    RegistrationForm,
    SetupForm,
    StepFormSet,
)
from .importing.extractors import detect_source_type, youtube_video_id
from .importing.normalizer import estimate_nutrition
from .locking import (
    CART_BROWSER_LOCK,
    REGISTRATION_LOCK,
    acquire_application_lock,
)
from .models import (
    BrowserLoginSession,
    CartAttempt,
    CartRun,
    Category,
    ImportJob,
    Recipe,
    RecipeRefinement,
    RecipeSlugAlias,
    RegistrationInvite,
    StorePreference,
)
from .services import (
    STORE_LINKS,
    build_shopping_items,
    get_selected_store,
    get_store_preferences,
    select_store,
)


SEARCH_CANDIDATE_LIMIT = 500
MAX_SEARCH_RANK_FRAGMENTS = 32
MAX_SEARCH_FRAGMENTS_PER_TOKEN = 4
MAX_SEARCH_QUERY_LENGTH = 120
MAX_SEARCH_TOKEN_LENGTH = 32
MAX_SEARCH_TOKENS = 8
REFINEMENT_HISTORY_LIMIT = 50
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
    maximum_distance = _search_maximum_distance(token)
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


def _search_maximum_distance(token: str) -> int:
    return 1 if len(token) <= 5 else 2 if len(token) <= 8 else 3


def _search_token_fragments(token: str) -> list[str]:
    """Return a bounded prefilter that cannot reject an allowed fuzzy match.

    A match may remove or replace at most ``maximum_distance`` characters.
    Selecting one more character than that guarantees at least one selected
    character remains. Insertions and transpositions preserve the characters.
    """
    fragment_count = min(
        len(token),
        _search_maximum_distance(token) + 1,
        MAX_SEARCH_FRAGMENTS_PER_TOKEN,
    )
    if fragment_count == 1:
        return [token[0]]
    indexes = [
        round(index * (len(token) - 1) / (fragment_count - 1))
        for index in range(fragment_count)
    ]
    return list(dict.fromkeys(token[index] for index in indexes))


def _search_rank_fragments(token: str) -> list[str]:
    bigrams = list(
        dict.fromkeys(
            token[index:index + 2] for index in range(max(0, len(token) - 1))
        )
    )
    prioritized = list(dict.fromkeys(token))
    if bigrams:
        prioritized.extend((bigrams[0], bigrams[-1], bigrams[len(bigrams) // 2]))
    return list(dict.fromkeys(prioritized))[:MAX_SEARCH_FRAGMENTS_PER_TOKEN]


def _search_fragment_score(query: str):
    fragments = []
    for token in _normalize_recipe_search(query).split()[:MAX_SEARCH_TOKENS]:
        fragments.extend(_search_rank_fragments(token))
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
    return sum(score_parts, Value(0, output_field=IntegerField()))


def _search_candidate_filter(query: str) -> Q:
    candidate_filter = Q()
    for token in _normalize_recipe_search(query).split()[:MAX_SEARCH_TOKENS]:
        token_filter = Q()
        for fragment in _search_token_fragments(token):
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
        # SQLite is the small development fallback. Avoid a coarse top-N cut
        # before the exact Python matcher because it has no word-similarity
        # function equivalent to PostgreSQL's pg_trgm implementation.
        return candidates.order_by("-updated_at")
    fragment_score = _search_fragment_score(query)
    zero = Value(0.0, output_field=FloatField())
    similarities = [
        Coalesce(Max(TrigramWordSimilarity(query, field)), zero)
        for field in SEARCH_FIELDS
    ]
    ranked = candidates.annotate(
        search_fragment_score=fragment_score,
        search_similarity=Greatest(*similarities)
    )
    word_similarity_pool = list(ranked.order_by(
        "-search_similarity", "-search_fragment_score", "-updated_at"
    )[:SEARCH_CANDIDATE_LIMIT])
    fragment_fallback_pool = list(ranked.order_by(
        "-search_fragment_score", "-search_similarity", "-updated_at"
    )[:SEARCH_CANDIDATE_LIMIT])
    return word_similarity_pool + fragment_fallback_pool


def _resolve_recipe_slug(slug: str, queryset=None) -> tuple[Recipe, bool]:
    queryset = queryset if queryset is not None else Recipe.objects.all()
    recipe = queryset.filter(slug=slug).first()
    if recipe is not None:
        return recipe, False
    recipe_id = (
        RecipeSlugAlias.objects.filter(slug=slug)
        .values_list("recipe_id", flat=True)
        .first()
    )
    if recipe_id is None:
        raise Http404
    recipe = queryset.filter(pk=recipe_id).first()
    if recipe is None:
        raise Http404
    return recipe, True


def _canonical_recipe_redirect(request, recipe: Recipe, view_name: str):
    url = reverse(view_name, args=[recipe.slug])
    query_string = request.META.get("QUERY_STRING", "")
    if query_string:
        url = f"{url}?{query_string}"
    return redirect(url, permanent=True)


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
@user_passes_test(lambda user: user.is_superuser)
@require_http_methods(["GET", "POST"])
def registration_access(request):
    now = timezone.now()
    RegistrationInvite.objects.filter(
        is_open=True,
        expires_at__lte=now,
    ).update(is_open=False, closed_at=now)
    active = RegistrationInvite.objects.filter(
        is_open=True,
        used_at__isnull=True,
        closed_at__isnull=True,
        expires_at__gt=now,
    ).first()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "open":
            token = secrets.token_urlsafe(32)
            with transaction.atomic():
                acquire_application_lock(REGISTRATION_LOCK)
                RegistrationInvite.objects.filter(is_open=True).update(
                    is_open=False,
                    closed_at=now,
                )
                active = RegistrationInvite.objects.create(
                    token_digest=RegistrationInvite.digest_token(token),
                    created_by=request.user,
                    expires_at=now + timedelta(hours=24),
                )
            request.session["registration_invite_token"] = token
            messages.success(request, "Одноразовая ссылка создана на 24 часа.")
        elif action == "close":
            with transaction.atomic():
                acquire_application_lock(REGISTRATION_LOCK)
                RegistrationInvite.objects.filter(
                    is_open=True,
                    used_at__isnull=True,
                    closed_at__isnull=True,
                ).update(is_open=False, closed_at=now)
            request.session.pop("registration_invite_token", None)
            messages.success(request, "Регистрация закрыта.")
        return redirect("registration-access")

    token = request.session.get("registration_invite_token", "")
    invite_url = ""
    if active and token and RegistrationInvite.digest_token(token) == active.token_digest:
        invite_url = request.build_absolute_uri(reverse("register-invite", args=[token]))
    latest = RegistrationInvite.objects.select_related("registered_user").first()
    return render(
        request,
        "registration/access.html",
        {"active_invite": active, "invite_url": invite_url, "latest_invite": latest},
    )


@require_http_methods(["GET", "POST"])
def register_invite(request, token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
        raise Http404
    digest = RegistrationInvite.digest_token(token)
    invite = RegistrationInvite.objects.filter(
        token_digest=digest,
        is_open=True,
        used_at__isnull=True,
        closed_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()
    if not invite:
        return render(request, "registration/invite_closed.html", status=410)
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            acquire_application_lock(REGISTRATION_LOCK)
            invite = RegistrationInvite.objects.select_for_update().filter(
                pk=invite.pk,
                is_open=True,
                used_at__isnull=True,
                closed_at__isnull=True,
                expires_at__gt=timezone.now(),
            ).first()
            if not invite:
                return render(request, "registration/invite_closed.html", status=410)
            user = form.save()
            invite.registered_user = user
            invite.used_at = timezone.now()
            invite.is_open = False
            invite.save(update_fields=["registered_user", "used_at", "is_open"])
        login(request, user)
        messages.success(request, "Аккаунт создан. Одноразовая регистрация закрыта.")
        return redirect("recipe-list")
    return render(request, "registration/register.html", {"form": form})


def _resume_cart_run_after_login(run: CartRun) -> None:
    if run.status not in {CartRun.Status.LOGIN_REQUIRED, CartRun.Status.FAILED}:
        return
    if run.cleanup_requested_at and not run.cleaned_at:
        run.status = CartRun.Status.CLEANUP_PENDING
        fields = ["status", "finished_at", "error"]
    else:
        if run.status == CartRun.Status.FAILED:
            retry_stores = {
                attempt.store
                for attempt in run.attempts.only("store", "status", "result")
                if attempt.status == CartAttempt.Status.BLOCKED
                or (
                    isinstance(attempt.result, dict)
                    and attempt.result.get("reason") == "store_unavailable"
                )
            }
            for index, store in enumerate(run.store_priority):
                if store in retry_stores:
                    run.next_store_index = index
                    break
        run.status = CartRun.Status.PENDING
        fields = ["status", "next_store_index", "finished_at", "error"]
    run.finished_at = None
    run.error = ""
    run.save(update_fields=fields)


def _record_browser_login_start_failure(
    login_session_id: int,
    error: Exception,
    *,
    cleanup_confirmed: bool,
) -> None:
    """Record failure without releasing the worker lock after uncertain cleanup."""
    try:
        with transaction.atomic():
            acquire_application_lock(CART_BROWSER_LOCK)
            login_session = BrowserLoginSession.objects.select_for_update().get(
                pk=login_session_id
            )
            login_session.error = str(error)[:500]
            fields = ["error"]
            if cleanup_confirmed:
                login_session.status = BrowserLoginSession.Status.FAILED
                login_session.finished_at = timezone.now()
                fields.extend(["status", "finished_at"])
            login_session.save(update_fields=fields)
    except Exception:
        # If the database is unavailable, the previously committed STARTING
        # row remains the conservative state and keeps the worker blocked.
        return


@login_required
def recipe_list(request):
    query = request.GET.get("q", "").strip()
    normalized_tokens = _normalize_recipe_search(query).split()
    if (
        len(query) > MAX_SEARCH_QUERY_LENGTH
        or len(normalized_tokens) > MAX_SEARCH_TOKENS
        or any(len(token) > MAX_SEARCH_TOKEN_LENGTH for token in normalized_tokens)
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
    recipes = Recipe.objects.select_related("created_by").prefetch_related(
        "ingredients", "steps", "import_jobs"
    )
    recipe, used_alias = _resolve_recipe_slug(slug, recipes)
    if used_alias and request.method in {"GET", "HEAD"}:
        return _canonical_recipe_redirect(request, recipe, "recipe-detail")
    all_ingredients = list(recipe.ingredients.all())
    if any(getattr(recipe, field) is None for field in NUTRITION_FIELDS):
        _fill_missing_recipe_calories(recipe, all_ingredients, save=False)
        recipe.calories_estimated = _nutrition_has_estimated_values(
            recipe, set(recipe.nutrition_manual_fields or [])
        )
    ingredients = [ingredient for ingredient in all_ingredients if not ingredient.is_water]
    import_job = getattr(recipe, "import_job", None) or next(
        iter(recipe.import_jobs.all()), None
    )
    video_id = (
        youtube_video_id(recipe.source_url)
        if import_job and import_job.source_type == ImportJob.SourceType.YOUTUBE
        else None
    )
    return render(
        request,
        "recipes/recipe_detail.html",
        {
            "recipe": recipe,
            "main_ingredients": [item for item in ingredients if not item.is_pantry],
            "pantry_ingredients": [item for item in ingredients if item.is_pantry],
            "source_import_job": import_job,
            "youtube_video_id": video_id,
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
    if preserve_fields is None:
        preserve_fields = set(recipe.nutrition_manual_fields or [])
    else:
        preserve_fields = set(preserve_fields)
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


def _nutrition_has_estimated_values(recipe, manual_fields: set[str]) -> bool:
    return any(
        field not in manual_fields and getattr(recipe, field) is not None
        for field in NUTRITION_FIELDS
    )


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


def _recipe_refinement_context(recipe):
    active_refinement = (
        recipe.refinements.select_related("requested_by")
        .filter(
            status__in=[
                RecipeRefinement.Status.PENDING,
                RecipeRefinement.Status.PROCESSING,
            ]
        )
        .order_by("-created_at", "-pk")
        .first()
    )
    history = recipe.refinements.select_related("requested_by")
    if active_refinement:
        history = history.exclude(pk=active_refinement.pk)
    refinements = list(
        history.order_by("-created_at", "-pk")[: (
            REFINEMENT_HISTORY_LIMIT - bool(active_refinement)
        )]
    )
    if active_refinement:
        refinements.append(active_refinement)
    refinements.sort(key=lambda item: (item.created_at, item.pk))
    return {
        "refinement_form": RecipeRefinementForm(),
        "refinements": refinements,
        "active_refinement": active_refinement,
    }


def _recipes_owned_by(user):
    recipes = Recipe.objects.all()
    if user.is_staff or user.is_superuser:
        return recipes
    return recipes.filter(created_by=user)


def _recipes_refinable_by(user):
    return _recipes_owned_by(user).filter(status=Recipe.Status.DRAFT)


def _can_refine_recipe(user, recipe):
    return bool(
        recipe.status == Recipe.Status.DRAFT
        and (user.is_staff or user.is_superuser or recipe.created_by_id == user.pk)
    )


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
            manual_fields = {
                field
                for field in NUTRITION_FIELDS
                if form.cleaned_data[field] is not None
            }
            _fill_missing_recipe_calories(
                recipe, save=True, preserve_fields=manual_fields
            )
            recipe.nutrition_manual_fields = sorted(manual_fields)
            recipe.calories_estimated = _nutrition_has_estimated_values(
                recipe, manual_fields
            )
            recipe.save(
                update_fields=[
                    "nutrition_manual_fields",
                    "calories_estimated",
                    "updated_at",
                ]
            )
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
    instance, used_alias = _resolve_recipe_slug(slug)
    if used_alias and request.method in {"GET", "HEAD"}:
        return _canonical_recipe_redirect(request, instance, "recipe-update")
    manual_fields = set(instance.nutrition_manual_fields or [])
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
                for field in calorie_fields.intersection(form.changed_data):
                    if form.cleaned_data[field] is None:
                        manual_fields.discard(field)
                    else:
                        manual_fields.add(field)
            if manual_calorie_change or recalculate:
                _fill_missing_recipe_calories(
                    recipe,
                    save=True,
                    overwrite=True,
                    preserve_fields=manual_fields,
                )
                recipe.nutrition_manual_fields = sorted(manual_fields)
                recipe.calories_estimated = _nutrition_has_estimated_values(
                    recipe, manual_fields
                )
                recipe.save(
                    update_fields=[
                        "nutrition_manual_fields",
                        "calories_estimated",
                        "updated_at",
                    ]
                )
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
            "can_refine_recipe": _can_refine_recipe(request.user, recipe),
            **(
                _recipe_refinement_context(recipe)
                if _can_refine_recipe(request.user, recipe)
                else {}
            ),
        },
    )


@login_required
@require_POST
def recipe_refine(request, slug):
    recipe, _ = _resolve_recipe_slug(slug, _recipes_refinable_by(request.user))
    form = RecipeRefinementForm(request.POST)
    active_refinement = recipe.refinements.filter(
        status__in=[
            RecipeRefinement.Status.PENDING,
            RecipeRefinement.Status.PROCESSING,
        ]
    ).exists()
    if active_refinement:
        messages.info(request, "Гермес уже перерабатывает этот рецепт.")
    elif form.is_valid():
        refinement = form.save(commit=False)
        refinement.recipe = recipe
        refinement.requested_by = request.user
        refinement.expected_recipe_updated_at = recipe.updated_at
        try:
            with transaction.atomic():
                refinement.save()
        except IntegrityError:
            messages.info(request, "Гермес уже перерабатывает этот рецепт.")
        else:
            messages.success(request, "Пожелание отправлено Гермесу.")
    else:
        messages.error(request, "Напишите пожелание к рецепту обычным текстом.")
    return redirect(f'{reverse("recipe-update", args=[recipe.slug])}#agent-chat')


@login_required
def recipe_refinement_status(request, slug, pk):
    recipe, _ = _resolve_recipe_slug(slug, _recipes_owned_by(request.user))
    refinement = get_object_or_404(RecipeRefinement, pk=pk, recipe=recipe)
    return JsonResponse(
        {
            "status": refinement.status,
            "status_label": refinement.get_status_display(),
            "error": refinement.error,
        }
    )


@login_required
@require_POST
def recipe_publish(request, slug):
    recipe, _ = _resolve_recipe_slug(
        slug, Recipe.objects.filter(status=Recipe.Status.DRAFT)
    )
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
    refinements = RecipeRefinement.objects.filter(
        requested_by=request.user
    ).select_related("recipe").order_by("-created_at")[:50]
    return render(
        request,
        "recipes/task_list.html",
        {
            "import_jobs": import_jobs,
            "refinements": refinements,
            "cart_runs": cart_runs,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def recipe_delete(request, slug):
    recipe, used_alias = _resolve_recipe_slug(slug)
    if used_alias and request.method in {"GET", "HEAD"}:
        return _canonical_recipe_redirect(request, recipe, "recipe-delete")
    if request.method == "POST":
        redirect_name = "draft-list" if recipe.is_draft else "recipe-list"
        recipe.delete()
        messages.success(request, "Рецепт удалён.")
        return redirect(redirect_name)
    return render(request, "recipes/recipe_confirm_delete.html", {"recipe": recipe})


@login_required
def shopping_list(request, slug):
    recipe, used_alias = _resolve_recipe_slug(
        slug, Recipe.objects.prefetch_related("ingredients")
    )
    if used_alias and request.method in {"GET", "HEAD"}:
        return _canonical_recipe_redirect(request, recipe, "shopping-list")
    try:
        servings = int(request.GET.get("servings", recipe.servings))
    except (TypeError, ValueError):
        servings = recipe.servings
    servings = max(1, min(servings, 100))
    selected_store = get_selected_store(request.user)
    store_options = [
        {
            "value": value,
            "label": label,
            "search_brand": STORE_LINKS[value][0],
            "search_place": STORE_LINKS[value][1],
        }
        for value, label in StorePreference.Store.choices
    ]
    items = build_shopping_items(recipe, servings, selected_store.store)
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
            "store_options": store_options,
            "selected_store": selected_store,
            "latest_cart_run": latest_cart_run,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def store_preferences(request):
    preferences = get_store_preferences(request.user)
    selected_store = next(
        (item for item in preferences if item.enabled), preferences[0]
    )
    if request.method == "POST":
        try:
            selected_store = select_store(request.user, request.POST.get("store", ""))
        except ValueError:
            messages.error(request, "Выберите магазин из списка.")
            return redirect("store-preferences")
        messages.success(
            request,
            f"Для заказа выбран магазин «{selected_store.get_store_display()}».",
        )
        next_url = request.POST.get("next", "")
        if next_url and url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return redirect(next_url)
        return redirect("store-preferences")
    return render(
        request,
        "recipes/store_preferences.html",
        {
            "store_options": StorePreference.Store.choices,
            "selected_store": selected_store,
        },
    )


@login_required
@require_POST
def cart_start(request, slug):
    recipe, _ = _resolve_recipe_slug(
        slug, Recipe.objects.prefetch_related("ingredients")
    )
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

    requested_store = request.POST.get("store", "")
    try:
        selected_store = select_store(request.user, requested_store)
    except ValueError:
        messages.error(request, "Выберите магазин для заказа.")
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
        store_priority=[selected_store.store],
        ingredient_snapshot=snapshot,
    )
    messages.success(
        request,
        f"Сборка запущена в магазине «{selected_store.get_store_display()}».",
    )
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
    matches_by_name = {}
    for match in status_attempt.matches.all() if status_attempt else []:
        matches_by_name.setdefault(match.ingredient_name.casefold(), []).append(match)
    quality_labels = {
        "exact": "Найдено полное совпадение",
        "substitute": "Найдена альтернатива",
        "missing": "Ничего не найдено",
        "queued": "В очереди",
        "unchecked": "Не проверено",
    }
    item_statuses = []
    is_waiting = run.status in {CartRun.Status.PENDING, CartRun.Status.PROCESSING}
    attempt_completed = status_attempt and status_attempt.status in {
        CartAttempt.Status.EXACT,
        CartAttempt.Status.SUBSTITUTIONS,
        CartAttempt.Status.INCOMPLETE,
    }
    for item in run.ingredient_snapshot:
        name = str(item.get("name", "")).strip()
        matching_items = matches_by_name.get(name.casefold(), [])
        match = matching_items.pop(0) if matching_items else None
        if match:
            quality = match.quality
        elif is_waiting:
            quality = "queued"
        elif attempt_completed:
            quality = "missing"
        else:
            quality = "unchecked"
        item_statuses.append(
            {
                "name": name,
                "quantity": " ".join(
                    part
                    for part in (str(item.get("quantity", "")), str(item.get("unit", "")))
                    if part
                ),
                "quality": quality,
                "label": quality_labels[quality],
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
    cleanup_pending = bool(run.cleanup_requested_at and not run.cleaned_at)
    _resume_cart_run_after_login(run)
    if cleanup_pending:
        messages.success(request, "Очистка снова поставлена в очередь.")
        return redirect("cart-detail", pk=run.pk)
    messages.success(request, "Попытка снова поставлена в очередь.")
    return redirect("cart-detail", pk=run.pk)


@login_required
@require_POST
def browser_login_start(request, pk=None):
    run = None
    if pk is not None:
        run = get_object_or_404(
            CartRun,
            pk=pk,
            requested_by=request.user,
            status__in=[CartRun.Status.LOGIN_REQUIRED, CartRun.Status.FAILED],
        )
    if not browser_login_is_configured():
        messages.error(request, "Удалённый вход в Яндекс Еду ещё не настроен.")
        return redirect("cart-detail", pk=run.pk) if run else redirect("store-preferences")

    now = timezone.now()
    remote_session_id = secrets.token_urlsafe(24)
    with transaction.atomic():
        acquire_application_lock(CART_BROWSER_LOCK)
        recovery_ready = reconcile_expired_browser_login_sessions()
        if not recovery_ready:
            messages.error(
                request,
                "Браузер ещё восстанавливается после прошлой сессии. Попробуйте позже.",
            )
            return redirect("cart-detail", pk=run.pk) if run else redirect("store-preferences")
        active = BrowserLoginSession.objects.select_for_update().filter(
            status__in=BROWSER_BLOCKING_STATUSES,
        ).first()
        if active:
            if active.user_id != request.user.id:
                messages.error(request, "Браузер сейчас занят. Попробуйте ещё раз чуть позже.")
                return redirect("cart-detail", pk=run.pk) if run else redirect("store-preferences")
            if run and active.run_id is None:
                active.run = run
                active.save(update_fields=["run"])
            elif run and active.run_id != run.id:
                messages.error(request, "Окно входа уже связано с другой сборкой.")
                return redirect("cart-detail", pk=run.pk)
            messages.info(request, "У вас уже открыто окно входа в Яндекс Еду.")
            return redirect("browser-login", pk=active.pk)
        if CartRun.objects.filter(
            status__in=[CartRun.Status.PROCESSING, CartRun.Status.CLEANING]
        ).exists():
            messages.error(request, "Дождитесь завершения текущей операции с корзиной.")
            return redirect("cart-detail", pk=run.pk) if run else redirect("store-preferences")

        # The Pi closes at the configured lifetime and probes every 15 seconds.
        # Keep the database lock slightly longer so the worker cannot overlap
        # the controller's timeout cleanup, even with modest clock skew.
        expires_at = now + timedelta(
            minutes=settings.CART_BROWSER_LOGIN_MINUTES,
            seconds=30,
        )
        login_session = BrowserLoginSession.objects.create(
            user=request.user,
            run=run,
            remote_session_id=remote_session_id,
            expires_at=expires_at,
        )

    try:
        start_browser_login_session(
            cart_browser_session_key(request.user.id),
            settings.CART_BROWSER_LOGIN_MINUTES,
            remote_session_id,
        )
    except BrowserLoginError as error:
        cleanup_confirmed = False
        try:
            stop_browser_login_session(remote_session_id)
            cleanup_confirmed = True
        except BrowserLoginError:
            pass
        _record_browser_login_start_failure(
            login_session.pk,
            error,
            cleanup_confirmed=cleanup_confirmed,
        )
        if cleanup_confirmed:
            messages.error(request, str(error))
        else:
            messages.error(
                request,
                "Запуск браузера завершился неопределённо. Новые операции с "
                "корзиной заблокированы до автоматического восстановления.",
            )
        return redirect("cart-detail", pk=run.pk) if run else redirect("store-preferences")

    try:
        with transaction.atomic():
            acquire_application_lock(CART_BROWSER_LOCK)
            login_session = BrowserLoginSession.objects.select_for_update().get(
                pk=login_session.pk,
                status=BrowserLoginSession.Status.STARTING,
            )
            login_session.status = BrowserLoginSession.Status.ACTIVE
            login_session.save(update_fields=["status"])
    except Exception as error:
        cleanup_confirmed = False
        try:
            stop_browser_login_session(remote_session_id)
            cleanup_confirmed = True
        except BrowserLoginError:
            pass
        _record_browser_login_start_failure(
            login_session.pk,
            error,
            cleanup_confirmed=cleanup_confirmed,
        )
        messages.error(
            request,
            "Браузер был закрыт после ошибки сохранения. Попробуйте ещё раз."
            if cleanup_confirmed
            else "Не удалось подтвердить закрытие браузера; сборка временно заблокирована.",
        )
        return redirect("cart-detail", pk=run.pk) if run else redirect("store-preferences")
    return redirect("browser-login", pk=login_session.pk)


@login_required
def browser_login(request, pk):
    login_session = get_object_or_404(
        BrowserLoginSession.objects.select_related("run"),
        pk=pk,
        user=request.user,
    )
    if login_session.status != BrowserLoginSession.Status.ACTIVE:
        messages.error(request, "Это окно входа уже закрыто.")
        return (
            redirect("cart-detail", pk=login_session.run_id)
            if login_session.run_id
            else redirect("store-preferences")
        )
    if login_session.expires_at <= timezone.now():
        with transaction.atomic():
            acquire_application_lock(CART_BROWSER_LOCK)
            recovery_ready = reconcile_expired_browser_login_sessions()
        if recovery_ready:
            messages.error(request, "Время ручного входа истекло. Запустите его ещё раз.")
        else:
            messages.error(request, "Сессия истекла, но браузер ещё безопасно закрывается.")
        return (
            redirect("cart-detail", pk=login_session.run_id)
            if login_session.run_id
            else redirect("store-preferences")
        )
    try:
        browser_url = issue_browser_login_access(login_session.remote_session_id)
    except BrowserLoginSessionNotFound:
        with transaction.atomic():
            acquire_application_lock(CART_BROWSER_LOCK)
            recovery_ready = reconcile_missing_browser_login_session(login_session.pk)
        if recovery_ready:
            messages.error(
                request,
                "Прошлое окно было закрыто при восстановлении браузера. "
                "Теперь можно сразу запустить новое.",
            )
        else:
            messages.error(
                request,
                "Не удалось подтвердить закрытие прошлого окна; браузер пока заблокирован.",
            )
        return (
            redirect("cart-detail", pk=login_session.run_id)
            if login_session.run_id
            else redirect("store-preferences")
        )
    except BrowserLoginError as error:
        messages.error(request, str(error))
        return (
            redirect("cart-detail", pk=login_session.run_id)
            if login_session.run_id
            else redirect("store-preferences")
        )
    return render(
        request,
        "recipes/browser_login.html",
        {"login_session": login_session, "browser_url": browser_url},
    )


@login_required
@require_POST
def browser_login_complete(request, pk):
    login_session = get_object_or_404(
        BrowserLoginSession.objects.select_related("run"),
        pk=pk,
        user=request.user,
        status=BrowserLoginSession.Status.ACTIVE,
    )
    try:
        stop_browser_login_session(login_session.remote_session_id)
    except BrowserLoginError as error:
        messages.error(request, f"Не удалось сохранить сессию: {error}")
        return redirect("browser-login", pk=login_session.pk)

    now = timezone.now()
    with transaction.atomic():
        acquire_application_lock(CART_BROWSER_LOCK)
        login_session.status = BrowserLoginSession.Status.COMPLETED
        login_session.finished_at = now
        login_session.error = ""
        login_session.save(update_fields=["status", "finished_at", "error"])
        if login_session.run_id:
            run = CartRun.objects.select_for_update().get(
                pk=login_session.run_id,
                requested_by=request.user,
            )
            _resume_cart_run_after_login(run)
    if login_session.run_id:
        messages.success(request, "Сессия Яндекса сохранена; сборка снова поставлена в очередь.")
        return redirect("cart-detail", pk=login_session.run_id)
    messages.success(request, "Сессия Яндекса сохранена для будущих сборок.")
    return redirect("store-preferences")


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

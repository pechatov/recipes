from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from recipes.forms import IngredientForm
from recipes.models import Category, ImportJob, Recipe, RecipeIngredient, RecipeStep
from recipes.views import _fill_missing_recipe_calories


class FirstRunTests(TestCase):
    def test_login_redirects_to_setup_without_users(self):
        response = self.client.get(reverse("login"))
        self.assertRedirects(response, reverse("setup-owner"))

    def test_setup_creates_owner_and_closes_itself(self):
        response = self.client.post(
            reverse("setup-owner"),
            {
                "username": "owner",
                "password1": "A-long-family-password-482!",
                "password2": "A-long-family-password-482!",
            },
        )
        self.assertRedirects(response, reverse("recipe-list"))
        owner = get_user_model().objects.get(username="owner")
        self.assertTrue(owner.is_superuser)
        self.assertTrue(owner.is_staff)

        second_attempt = self.client.get(reverse("setup-owner"))
        self.assertRedirects(second_attempt, reverse("recipe-list"))


class RecipeViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="cook", password="safe-test-pass")
        self.recipe = Recipe.objects.create(title="Семейная паста", servings=2, created_by=self.user)
        RecipeIngredient.objects.create(
            recipe=self.recipe,
            name="Сливки",
            quantity="200",
            unit="мл",
            search_query="сливки 20%",
        )
        RecipeStep.objects.create(recipe=self.recipe, title="Соус", instruction="Прогреть сливки")

    def test_recipe_list_requires_login(self):
        response = self.client.get(reverse("recipe-list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_authenticated_user_can_view_recipe(self):
        self.client.force_login(self.user)
        response = self.client.get(self.recipe.get_absolute_url())
        self.assertContains(response, "Семейная паста")
        self.assertContains(response, "Прогреть сливки")
        self.assertEqual(str(response.context["recipe"].calories_per_serving), "205.0")
        self.assertEqual(str(response.context["recipe"].calories_per_100g), "205.0")

    def test_estimated_calories_can_be_recalculated_after_ingredient_changes(self):
        _fill_missing_recipe_calories(self.recipe, save=True)
        ingredient = self.recipe.ingredients.get()
        ingredient.quantity = 400
        ingredient.save(update_fields=["quantity"])

        _fill_missing_recipe_calories(self.recipe, save=True, overwrite=True)

        self.recipe.refresh_from_db()
        self.assertEqual(str(self.recipe.calories_per_serving), "410.0")
        self.assertEqual(str(self.recipe.calories_per_100g), "205.0")

    def test_pantry_toggle_does_not_overwrite_manual_calories(self):
        self.recipe.calories_per_serving = 999
        self.recipe.calories_per_100g = 888
        self.recipe.calories_estimated = False
        self.recipe.save(
            update_fields=[
                "calories_per_serving",
                "calories_per_100g",
                "calories_estimated",
            ]
        )
        ingredient = self.recipe.ingredients.get()
        step = self.recipe.steps.get()
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("recipe-update", args=[self.recipe.slug]),
            {
                "title": self.recipe.title,
                "description": self.recipe.description,
                "servings": self.recipe.servings,
                "prep_minutes": self.recipe.prep_minutes,
                "cook_minutes": self.recipe.cook_minutes,
                "calories_per_serving": "999",
                "calories_per_100g": "888",
                "ingredients-TOTAL_FORMS": 1,
                "ingredients-INITIAL_FORMS": 1,
                "ingredients-MIN_NUM_FORMS": 1,
                "ingredients-MAX_NUM_FORMS": 1000,
                "ingredients-0-id": ingredient.pk,
                "ingredients-0-section": ingredient.section,
                "ingredients-0-name": ingredient.name,
                "ingredients-0-quantity": ingredient.quantity,
                "ingredients-0-unit": ingredient.unit,
                "ingredients-0-search_query": ingredient.search_query,
                "ingredients-0-is_pantry": "on",
                "steps-TOTAL_FORMS": 1,
                "steps-INITIAL_FORMS": 1,
                "steps-MIN_NUM_FORMS": 1,
                "steps-MAX_NUM_FORMS": 1000,
                "steps-0-id": step.pk,
                "steps-0-section": step.section,
                "steps-0-title": step.title,
                "steps-0-instruction": step.instruction,
            },
        )

        self.assertRedirects(response, self.recipe.get_absolute_url())
        self.recipe.refresh_from_db()
        ingredient.refresh_from_db()
        self.assertTrue(ingredient.is_pantry)
        self.assertEqual(str(self.recipe.calories_per_serving), "999.0")
        self.assertEqual(str(self.recipe.calories_per_100g), "888.0")
        self.assertFalse(self.recipe.calories_estimated)

    def test_edit_form_has_clipboard_zones_for_cover_and_step_images(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("recipe-update", args=[self.recipe.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.content.count(b"data-image-paste-zone"), 3)
        self.assertContains(response, "Кликните сюда и нажмите Ctrl+V")
        self.assertContains(response, "image/jpeg,image/png,image/webp")

    def test_shopping_page_scales_and_links_to_lavka(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("shopping-list", args=[self.recipe.slug]), {"servings": 4})
        self.assertContains(response, "400 мл")
        self.assertContains(response, "https://lavka.yandex.ru/search?text=")
        self.assertContains(response, "%D1%81%D0%BB%D0%B8%D0%B2%D0%BA%D0%B8+20%25")

    def test_search_finds_recipe_by_ingredient(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("recipe-list"), {"q": "сливки"})
        self.assertContains(response, "Семейная паста")

    def test_fuzzy_query_keeps_candidates_for_client_side_filtering(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("recipe-list"), {"q": "слвк"})

        self.assertContains(response, "Семейная паста")

    def test_fuzzy_search_keeps_matches_alongside_exact_match(self):
        exact = Recipe.objects.create(title="СЛВК — семейная заметка", created_by=self.user)
        self.client.force_login(self.user)

        response = self.client.get(reverse("recipe-list"), {"q": "слвк"})

        self.assertContains(response, exact.title)
        self.assertContains(response, self.recipe.title)

    def test_fuzzy_search_caps_python_candidate_processing(self):
        for index in range(8):
            Recipe.objects.create(title=f"Сливовый рецепт {index}", created_by=self.user)
        self.client.force_login(self.user)

        with patch("recipes.views.SEARCH_CANDIDATE_LIMIT", 5), patch(
            "recipes.views._recipe_matches_fuzzy_query", return_value=False
        ) as matcher:
            self.client.get(reverse("recipe-list"), {"q": "слив"})

        self.assertEqual(matcher.call_count, 5)

    def test_fuzzy_search_rejects_input_before_building_an_unbounded_query(self):
        self.client.force_login(self.user)

        for query in ("а" * 121, "один два три четыре пять шесть семь восемь девять"):
            with self.subTest(query=query), patch(
                "recipes.views._search_candidate_filter"
            ) as candidate_filter:
                response = self.client.get(reverse("recipe-list"), {"q": query})

            self.assertEqual(response.status_code, 400)
            candidate_filter.assert_not_called()

    def test_server_search_excludes_unrelated_recipes_without_javascript(self):
        other = Recipe.objects.create(title="Яблочный пирог", created_by=self.user)
        RecipeIngredient.objects.create(
            recipe=other,
            name="Яблоки",
            quantity=3,
            unit="шт.",
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("recipe-list"), {"q": "сливки"})

        self.assertContains(response, "Семейная паста")
        self.assertNotContains(response, "Яблочный пирог")
        self.assertContains(response, 'data-server-filtered="true"')

    def test_water_is_hidden_without_deleting_historical_quantity(self):
        water = RecipeIngredient.objects.create(
            recipe=self.recipe,
            name="Горячая вода",
            quantity=500,
            unit="мл",
        )
        self.client.force_login(self.user)

        response = self.client.get(self.recipe.get_absolute_url())

        self.assertNotContains(response, "Горячая вода")
        self.assertEqual(str(response.context["recipe"].calories_per_100g), "58.6")
        self.assertTrue(RecipeIngredient.objects.filter(pk=water.pk).exists())

    def test_unchanged_historical_water_does_not_block_ingredient_edit_form(self):
        water = RecipeIngredient.objects.create(
            recipe=self.recipe,
            name="Горячая вода",
            quantity=500,
            unit="мл",
        )
        form = IngredientForm(
            data={"name": "Горячая вода", "quantity": 500, "unit": "мл"},
            instance=water,
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(IngredientForm(data={"name": "Горячая вода"}).is_valid())

    def test_recipe_list_filters_by_category(self):
        soup_category = Category.objects.get(slug="soup")
        salad_category = Category.objects.get(slug="salad")
        self.recipe.categories.add(soup_category)
        salad = Recipe.objects.create(title="Овощной салат", created_by=self.user)
        salad.categories.add(salad_category)
        self.client.force_login(self.user)

        response = self.client.get(reverse("recipe-list"), {"category": "soup"})
        self.assertContains(response, "Семейная паста")
        self.assertNotContains(response, "Овощной салат")
        self.assertContains(response, "Суп")

    def test_recipe_list_filters_by_author(self):
        other = get_user_model().objects.create_user(username="guest")
        other_recipe = Recipe.objects.create(title="Чужой пирог", created_by=other)
        self.client.force_login(self.user)

        response = self.client.get(reverse("recipe-list"), {"author": self.user.pk})

        self.assertContains(response, self.recipe.title)
        self.assertNotContains(response, other_recipe.title)
        self.assertContains(response, self.user.username)

    def test_invalid_servings_falls_back_to_recipe_servings(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("shopping-list", args=[self.recipe.slug]), {"servings": "oops"})
        self.assertEqual(response.context["servings"], 2)

    def test_create_recipe_accepts_one_filled_and_one_empty_extra_form(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("recipe-create"),
            {
                "title": "Новый суп",
                "description": "",
                "servings": 4,
                "prep_minutes": 5,
                "cook_minutes": 30,
                "ingredients-TOTAL_FORMS": 2,
                "ingredients-INITIAL_FORMS": 0,
                "ingredients-MIN_NUM_FORMS": 1,
                "ingredients-MAX_NUM_FORMS": 1000,
                "ingredients-0-name": "Картофель",
                "ingredients-0-quantity": 500,
                "ingredients-0-unit": "г",
                "ingredients-0-note": "",
                "ingredients-0-search_query": "картофель",
                "ingredients-1-name": "",
                "ingredients-1-quantity": "",
                "ingredients-1-unit": "",
                "ingredients-1-note": "",
                "ingredients-1-search_query": "",
                "steps-TOTAL_FORMS": 2,
                "steps-INITIAL_FORMS": 0,
                "steps-MIN_NUM_FORMS": 1,
                "steps-MAX_NUM_FORMS": 1000,
                "steps-0-title": "Варка",
                "steps-0-instruction": "Сварить до мягкости.",
                "steps-1-title": "",
                "steps-1-instruction": "",
            },
        )

        created = Recipe.objects.get(title="Новый суп")
        self.assertRedirects(response, created.get_absolute_url())
        self.assertEqual(created.ingredients.count(), 1)
        self.assertEqual(created.steps.count(), 1)
        self.assertEqual(str(created.calories_per_serving), "96.2")
        self.assertEqual(str(created.calories_per_100g), "77.0")

    def test_create_recipe_does_not_mix_manual_and_estimated_calories(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("recipe-create"),
            {
                "title": "Суп с ручной калорийностью",
                "servings": 4,
                "prep_minutes": 5,
                "cook_minutes": 30,
                "calories_per_serving": "123",
                "calories_per_100g": "",
                "ingredients-TOTAL_FORMS": 1,
                "ingredients-INITIAL_FORMS": 0,
                "ingredients-MIN_NUM_FORMS": 1,
                "ingredients-MAX_NUM_FORMS": 1000,
                "ingredients-0-name": "Картофель",
                "ingredients-0-quantity": 500,
                "ingredients-0-unit": "г",
                "steps-TOTAL_FORMS": 1,
                "steps-INITIAL_FORMS": 0,
                "steps-MIN_NUM_FORMS": 1,
                "steps-MAX_NUM_FORMS": 1000,
                "steps-0-title": "Варка",
                "steps-0-instruction": "Сварить до мягкости.",
            },
        )

        created = Recipe.objects.get(title="Суп с ручной калорийностью")
        self.assertRedirects(response, created.get_absolute_url())
        self.assertEqual(str(created.calories_per_serving), "123.0")
        self.assertIsNone(created.calories_per_100g)
        self.assertFalse(created.calories_estimated)
        detail = self.client.get(created.get_absolute_url())
        self.assertIsNone(detail.context["recipe"].calories_per_100g)

    def test_manual_calorie_edit_clears_unchanged_estimated_pair(self):
        _fill_missing_recipe_calories(self.recipe, save=True)
        self.recipe.calories_estimated = True
        self.recipe.save(update_fields=["calories_estimated", "updated_at"])
        ingredient = self.recipe.ingredients.get()
        step = self.recipe.steps.get()
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("recipe-update", args=[self.recipe.slug]),
            {
                "title": self.recipe.title,
                "description": self.recipe.description,
                "servings": self.recipe.servings,
                "prep_minutes": self.recipe.prep_minutes,
                "cook_minutes": self.recipe.cook_minutes,
                "calories_per_serving": "999",
                "calories_per_100g": self.recipe.calories_per_100g,
                "ingredients-TOTAL_FORMS": 1,
                "ingredients-INITIAL_FORMS": 1,
                "ingredients-MIN_NUM_FORMS": 1,
                "ingredients-MAX_NUM_FORMS": 1000,
                "ingredients-0-id": ingredient.pk,
                "ingredients-0-name": ingredient.name,
                "ingredients-0-quantity": ingredient.quantity,
                "ingredients-0-unit": ingredient.unit,
                "ingredients-0-search_query": ingredient.search_query,
                "steps-TOTAL_FORMS": 1,
                "steps-INITIAL_FORMS": 1,
                "steps-MIN_NUM_FORMS": 1,
                "steps-MAX_NUM_FORMS": 1000,
                "steps-0-id": step.pk,
                "steps-0-title": step.title,
                "steps-0-instruction": step.instruction,
            },
        )

        self.assertRedirects(response, self.recipe.get_absolute_url())
        self.recipe.refresh_from_db()
        self.assertEqual(str(self.recipe.calories_per_serving), "999.0")
        self.assertIsNone(self.recipe.calories_per_100g)
        self.assertFalse(self.recipe.calories_estimated)

    def test_draft_is_hidden_until_published(self):
        draft = Recipe.objects.create(
            title="Черновой пирог",
            status=Recipe.Status.DRAFT,
            created_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("recipe-list"))
        self.assertNotContains(response, draft.title)
        drafts = self.client.get(reverse("draft-list"))
        self.assertContains(drafts, draft.title)

        get_publish = self.client.get(reverse("recipe-publish", args=[draft.slug]))
        self.assertEqual(get_publish.status_code, 405)
        response = self.client.post(reverse("recipe-publish", args=[draft.slug]))
        self.assertRedirects(response, draft.get_absolute_url())
        draft.refresh_from_db()
        self.assertEqual(draft.status, Recipe.Status.PUBLISHED)

    def test_import_url_creates_queued_job(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("import-create"),
            {
                "source_url": "https://youtu.be/dQw4w9WgXcQ",
                "custom_prompt": "Сохрани острые ингредиенты",
            },
        )
        job = ImportJob.objects.get()
        self.assertRedirects(response, reverse("import-detail", args=[job.pk]))
        self.assertEqual(job.source_type, ImportJob.SourceType.YOUTUBE)
        self.assertEqual(job.status, ImportJob.Status.PENDING)
        self.assertEqual(job.requested_by, self.user)
        self.assertEqual(job.custom_prompt, "Сохрани острые ингредиенты")

    def test_task_list_shows_current_users_imports_and_carts(self):
        other = get_user_model().objects.create_user(username="guest")
        own_job = ImportJob.objects.create(
            source_url="https://example.com/own",
            source_type=ImportJob.SourceType.WEBSITE,
            requested_by=self.user,
        )
        ImportJob.objects.create(
            source_url="https://example.com/other",
            source_type=ImportJob.SourceType.WEBSITE,
            requested_by=other,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("task-list"))

        self.assertContains(response, reverse("import-detail", args=[own_job.pk]))
        self.assertNotContains(response, "https://example.com/other")

    def test_completed_draft_can_be_queued_for_reprocessing(self):
        draft = Recipe.objects.create(
            title="Суп с гренками",
            status=Recipe.Status.DRAFT,
            created_by=self.user,
        )
        job = ImportJob.objects.create(
            source_url="https://example.com/soup",
            source_type=ImportJob.SourceType.WEBSITE,
            status=ImportJob.Status.COMPLETED,
            recipe=draft,
            requested_by=self.user,
        )
        self.client.force_login(self.user)
        response = self.client.post(reverse("import-reprocess", args=[job.pk]))
        self.assertRedirects(response, reverse("import-detail", args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.PENDING)
        self.assertEqual(job.recipe, draft)

    def test_completed_import_cannot_be_reprocessed_after_partial_publication(self):
        published = Recipe.objects.create(
            title="Уже опубликован",
            status=Recipe.Status.PUBLISHED,
            created_by=self.user,
        )
        draft = Recipe.objects.create(
            title="Оставшийся черновик",
            status=Recipe.Status.DRAFT,
            created_by=self.user,
        )
        job = ImportJob.objects.create(
            source_url="https://example.com/menu",
            source_type=ImportJob.SourceType.WEBSITE,
            status=ImportJob.Status.COMPLETED,
            recipe=published,
            requested_by=self.user,
        )
        job.recipes.add(published, draft)
        self.client.force_login(self.user)

        response = self.client.post(reverse("import-reprocess", args=[job.pk]), follow=True)

        self.assertRedirects(response, reverse("import-detail", args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)
        self.assertContains(response, "один из рецептов уже опубликован")

    def test_import_endpoints_are_private_to_requesting_user(self):
        other = get_user_model().objects.create_user(username="private-importer")
        draft = Recipe.objects.create(
            title="Чужой черновик",
            status=Recipe.Status.DRAFT,
            created_by=other,
        )
        completed = ImportJob.objects.create(
            source_url="https://example.com/private-completed",
            source_type=ImportJob.SourceType.WEBSITE,
            status=ImportJob.Status.COMPLETED,
            recipe=draft,
            requested_by=other,
        )
        completed.recipes.add(draft)
        failed = ImportJob.objects.create(
            source_url="https://example.com/private-failed",
            source_type=ImportJob.SourceType.WEBSITE,
            status=ImportJob.Status.FAILED,
            requested_by=other,
        )
        self.client.force_login(self.user)

        self.assertEqual(
            self.client.get(reverse("import-detail", args=[completed.pk])).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse("import-reprocess", args=[completed.pk])).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse("import-retry", args=[failed.pk])).status_code,
            404,
        )
        completed.refresh_from_db()
        failed.refresh_from_db()
        self.assertEqual(completed.status, ImportJob.Status.COMPLETED)
        self.assertEqual(failed.status, ImportJob.Status.FAILED)

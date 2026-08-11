from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from recipes.models import Category, ImportJob, Recipe, RecipeIngredient, RecipeStep


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
            {"source_url": "https://youtu.be/dQw4w9WgXcQ"},
        )
        job = ImportJob.objects.get()
        self.assertRedirects(response, reverse("import-detail", args=[job.pk]))
        self.assertEqual(job.source_type, ImportJob.SourceType.YOUTUBE)
        self.assertEqual(job.status, ImportJob.Status.PENDING)
        self.assertEqual(job.requested_by, self.user)

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

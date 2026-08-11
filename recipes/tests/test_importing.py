from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from recipes.importing.exceptions import SourceError
from recipes.importing.exceptions import AIResponseError
from recipes.importing.extractors import (
    SourceDocument,
    _validate_public_url,
    extract_website,
    youtube_video_id,
)
from recipes.importing.normalizer import normalize_recipe
from recipes.importing.pipeline import process_import_job
from recipes.models import ImportJob, Recipe


class ExtractorTests(TestCase):
    def test_recognizes_common_youtube_urls(self):
        self.assertEqual(youtube_video_id("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(
            youtube_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10"),
            "dQw4w9WgXcQ",
        )
        self.assertEqual(
            youtube_video_id("https://youtube.com/shorts/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_rejects_local_source_addresses(self):
        for url in ("http://127.0.0.1/secret", "http://[::1]/", "http://192.168.1.10/"):
            with self.subTest(url=url), self.assertRaises(SourceError):
                _validate_public_url(url)

    @patch("recipes.importing.extractors._download_html")
    def test_extracts_recipe_json_ld_and_readable_text(self, download):
        download.return_value = (
            """<html><head><title>Лишний title</title>
            <script type="application/ld+json">{
              "@context": "https://schema.org", "@type": "Recipe",
              "name": "Блины", "recipeIngredient": ["200 мл молоко"],
              "recipeInstructions": [{"@type": "HowToStep", "text": "Смешать и пожарить."}]
            }</script></head><body><nav>Меню</nav><h1>Домашние блины</h1>
            <p>Подробное описание приготовления домашних блинов с советами для всей семьи.</p>
            <p>Смешайте продукты и обжарьте тесто порциями на хорошо прогретой сковороде.</p>
            </body></html>""",
            "https://example.com/blini",
        )
        document = extract_website("https://example.com/blini")
        self.assertEqual(document.title, "Домашние блины")
        self.assertEqual(document.structured_recipe["name"], "Блины")
        self.assertNotIn("Меню", document.text)


class NormalizerTests(TestCase):
    def test_normalizes_limits_and_decimal_quantity(self):
        recipe = normalize_recipe(
            {
                "title": "  Паста   с грибами ",
                "servings": 4,
                "prep_minutes": -5,
                "ingredients": [
                    {"name": " Шампиньоны ", "quantity": "250,5", "unit": "г"}
                ],
                "steps": [{"title": "Соус", "instruction": "  Обжарить   грибы. "}],
            }
        )
        self.assertEqual(recipe["title"], "Паста с грибами")
        self.assertEqual(recipe["ingredients"][0]["quantity"], "250.50")
        self.assertEqual(recipe["prep_minutes"], 0)

    def test_ai_recipe_requires_quantity_and_unit(self):
        with self.assertRaisesRegex(AIResponseError, "Соль"):
            normalize_recipe(
                {
                    "title": "Суп",
                    "ingredients": [{"section": "Для супа", "name": "Соль"}],
                    "steps": [{"instruction": "Сварить."}],
                },
                require_quantities=True,
            )

    def test_keeps_component_section_and_estimate_marker(self):
        recipe = normalize_recipe(
            {
                "title": "Суп с гренками",
                "ingredients": [
                    {
                        "section": "Для гренок",
                        "name": "Хлеб",
                        "quantity": 200,
                        "unit": "г",
                        "estimated": True,
                    }
                ],
                "steps": [{"instruction": "Подсушить хлеб."}],
            },
            require_quantities=True,
        )
        self.assertEqual(recipe["ingredients"][0]["section"], "Для гренок")
        self.assertTrue(recipe["ingredients"][0]["estimated"])

    def test_ai_import_discards_ingredient_preparation_notes(self):
        recipe = normalize_recipe(
            {
                "title": "Гороховый суп",
                "ingredients": [
                    {
                        "name": "Горох колотый сухой",
                        "quantity": 300,
                        "unit": "г",
                        "note": "Замочить на ночь и разделить пополам",
                    }
                ],
                "steps": [{"instruction": "Замочить горох на ночь."}],
            },
            require_quantities=True,
            keep_ingredient_notes=False,
        )
        self.assertEqual(recipe["ingredients"][0]["note"], "")

    def test_ai_recipe_requires_known_category(self):
        with self.assertRaisesRegex(AIResponseError, "категор"):
            normalize_recipe(
                {
                    "title": "Суп",
                    "categories": ["горячее"],
                    "ingredients": [{"name": "Вода", "quantity": 1000, "unit": "мл"}],
                    "steps": [{"instruction": "Сварить."}],
                },
                require_quantities=True,
                require_categories=True,
            )

    def test_soup_is_not_also_classified_as_main_course(self):
        recipe = normalize_recipe(
            {
                "title": "Гороховый суп с гренками",
                "categories": ["soup", "main-course"],
                "ingredients": [{"name": "Горох", "quantity": 300, "unit": "г"}],
                "steps": [{"instruction": "Сварить суп."}],
            },
            require_quantities=True,
            require_categories=True,
        )
        self.assertEqual(recipe["categories"], ["soup"])


class PipelineTests(TestCase):
    @override_settings(RECIPE_AI_BASE_URL="", RECIPE_AI_MODEL="")
    @patch("recipes.importing.pipeline.extract_source")
    def test_structured_page_creates_editable_draft_without_ai(self, extract_source):
        extract_source.return_value = SourceDocument(
            source_type="website",
            title="Источник",
            text="Длинное описание источника",
            structured_recipe={
                "@type": "Recipe",
                "name": "Картофельный суп",
                "description": "Простой домашний суп",
                "recipeYield": "4 порции",
                "prepTime": "PT10M",
                "cookTime": "PT30M",
                "recipeIngredient": ["500 г картофель", "1 шт. морковь"],
                "recipeInstructions": [
                    {"@type": "HowToStep", "name": "Подготовка", "text": "Нарезать овощи."},
                    {"@type": "HowToStep", "text": "Варить до мягкости."},
                ],
            },
        )
        user = get_user_model().objects.create_user("importer")
        job = ImportJob.objects.create(
            source_url="https://example.com/soup",
            source_type=ImportJob.SourceType.WEBSITE,
            requested_by=user,
        )

        recipe = process_import_job(job)
        job.refresh_from_db()
        self.assertEqual(recipe.status, Recipe.Status.DRAFT)
        self.assertEqual(recipe.created_by, user)
        self.assertEqual(recipe.ingredients.count(), 2)
        self.assertEqual(recipe.steps.count(), 2)
        self.assertEqual(list(recipe.categories.values_list("slug", flat=True)), ["soup"])
        self.assertEqual(job.status, ImportJob.Status.COMPLETED)
        self.assertEqual(job.recipe, recipe)

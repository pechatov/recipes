from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from recipes.models import Recipe, RecipeIngredient
from recipes.services import build_lavka_search_url, build_shopping_items, build_store_search_url


class ShoppingServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="cook", password="safe-test-pass")
        self.recipe = Recipe.objects.create(title="Паста", servings=2, created_by=self.user)

    def test_lavka_url_uses_encoded_search_query(self):
        url = build_lavka_search_url("сливки 20%")
        self.assertEqual(
            url,
            "https://lavka.yandex.ru/search?text=%D1%81%D0%BB%D0%B8%D0%B2%D0%BA%D0%B8+20%25",
        )

    def test_priority_store_url_uses_encoded_search_query(self):
        self.assertEqual(
            build_store_search_url("auchan", "сливки 20%"),
            "https://www.auchan.ru/catalog/?q=%D1%81%D0%BB%D0%B8%D0%B2%D0%BA%D0%B8+20%25",
        )

    def test_shopping_items_scale_quantities(self):
        ingredient = RecipeIngredient.objects.create(
            recipe=self.recipe,
            name="Сливки",
            quantity=Decimal("200"),
            unit="мл",
            search_query="сливки 20%",
        )

        item = build_shopping_items(self.recipe, servings=5)[0]

        self.assertEqual(item.ingredient, ingredient)
        self.assertEqual(item.quantity, Decimal("500"))
        self.assertEqual(item.display_quantity, "500")
        self.assertIn("%D1%81%D0%BB%D0%B8%D0%B2%D0%BA%D0%B8+20%25", item.search_url)

    def test_missing_quantity_remains_unquantified(self):
        RecipeIngredient.objects.create(recipe=self.recipe, name="Соль")

        item = build_shopping_items(self.recipe, servings=4)[0]

        self.assertIsNone(item.quantity)
        self.assertEqual(item.display_quantity, "")

    def test_plain_water_is_not_a_shopping_item(self):
        RecipeIngredient.objects.create(
            recipe=self.recipe,
            name="Горячая вода",
            quantity=Decimal("500"),
            unit="мл",
        )
        RecipeIngredient.objects.create(
            recipe=self.recipe,
            name="Лёд в кубиках",
            quantity=Decimal("6"),
            unit="шт.",
        )

        self.assertEqual(build_shopping_items(self.recipe, servings=2), [])

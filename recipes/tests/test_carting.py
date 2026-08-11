from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from recipes.carting.pipeline import process_cart_run
from recipes.carting.client import STORE_INSTRUCTIONS, cart_browser_session_key
from recipes.models import (
    CartAttempt,
    CartItemMatch,
    CartRun,
    Recipe,
    RecipeIngredient,
    StorePreference,
)
from recipes.services import get_store_preferences


class CartViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="cook", password="safe-pass")
        self.other_user = get_user_model().objects.create_user(
            username="other", password="safe-pass"
        )
        self.recipe = Recipe.objects.create(title="Суп", servings=2, created_by=self.user)
        self.potato = RecipeIngredient.objects.create(
            recipe=self.recipe,
            name="Картофель",
            quantity=400,
            unit="г",
            search_query="картофель",
        )
        self.salt = RecipeIngredient.objects.create(
            recipe=self.recipe,
            name="Соль",
            quantity=5,
            unit="г",
            optional=True,
        )
        self.client.force_login(self.user)

    def test_default_store_priority_matches_requested_order(self):
        preferences = get_store_preferences(self.user)
        self.assertEqual(
            [item.store for item in preferences],
            ["auchan", "perekrestok", "pyaterochka", "magnit", "lavka"],
        )

    def test_start_cart_snapshots_selected_scaled_ingredients(self):
        response = self.client.post(
            reverse("cart-start", args=[self.recipe.slug]),
            {"servings": 4, "ingredients": [str(self.potato.pk)]},
        )
        run = CartRun.objects.get()
        self.assertRedirects(response, reverse("cart-detail", args=[run.pk]))
        self.assertEqual(run.store_priority[0], StorePreference.Store.AUCHAN)
        self.assertEqual(run.ingredient_snapshot[0]["name"], "Картофель")
        self.assertEqual(run.ingredient_snapshot[0]["quantity"], "800")
        self.assertNotContains(self.client.get(reverse("cart-detail", args=[run.pk])), "Соль")

    def test_cart_run_is_private_to_requesting_user(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.other_user,
            servings=2,
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )
        response = self.client.get(reverse("cart-detail", args=[run.pk]))
        self.assertEqual(response.status_code, 404)

    def test_login_instructions_are_scoped_to_requesting_user(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["lavka"],
            ingredient_snapshot=[],
        )

        response = self.client.get(reverse("cart-detail", args=[run.pk]))

        self.assertContains(
            response,
            f"./scripts/cart-browser-login-pi.sh start {self.user.pk}",
        )
        self.assertNotContains(
            response,
            f"./scripts/cart-browser-login-pi.sh start {self.other_user.pk}",
        )

    def test_store_preferences_can_disable_and_reorder_stores(self):
        get_store_preferences(self.user)
        response = self.client.post(
            reverse("store-preferences"),
            {
                "enabled_lavka": "on",
                "position_lavka": "0",
                "position_auchan": "4",
                "position_perekrestok": "3",
                "position_pyaterochka": "2",
                "position_magnit": "1",
            },
        )
        self.assertRedirects(response, reverse("store-preferences"))
        enabled = list(
            self.user.store_preferences.filter(enabled=True).values_list("store", flat=True)
        )
        self.assertEqual(enabled, ["lavka"])

    def test_retry_of_old_captcha_failure_resumes_blocked_store(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.FAILED,
            store_priority=["auchan", "perekrestok", "lavka"],
            ingredient_snapshot=[],
            next_store_index=3,
        )
        CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.BLOCKED,
        )

        response = self.client.post(reverse("cart-retry", args=[run.pk]))

        self.assertRedirects(response, reverse("cart-detail", args=[run.pk]))
        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.PENDING)
        self.assertEqual(run.next_store_index, 0)


class CartPipelineTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="cook")
        self.recipe = Recipe.objects.create(title="Паста", servings=2, created_by=self.user)
        self.snapshot = [
            {
                "name": "Спагетти",
                "quantity": "400",
                "unit": "г",
                "section": "",
                "search_query": "спагетти",
                "optional": False,
            }
        ]

    def make_run(self, stores=None):
        return CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PROCESSING,
            store_priority=stores or ["auchan"],
            ingredient_snapshot=self.snapshot,
        )

    def test_all_stores_use_yandex_food_retail(self):
        self.assertEqual(
            {instructions[1] for instructions in STORE_INSTRUCTIONS.values()},
            {"https://eda.yandex.ru/retail"},
        )
        self.assertIn("pyaterochka", STORE_INSTRUCTIONS)
        self.assertNotIn("kuper.ru", repr(STORE_INSTRUCTIONS))

    def test_browser_session_key_is_stable_and_user_specific(self):
        self.assertEqual(cart_browser_session_key(self.user.pk), "recipes-cart-user-1")
        other_user = get_user_model().objects.create_user(username="other")
        self.assertNotEqual(
            cart_browser_session_key(self.user.pk),
            cart_browser_session_key(other_user.pk),
        )

    @patch("recipes.carting.pipeline.assemble_store_cart")
    @patch("recipes.carting.pipeline.inspect_store_cart")
    def test_exact_cart_completes_run(self, inspect, assemble):
        result = {
            "status": "exact",
            "cart_url": "https://kuper.ru/cart",
            "summary": "Всё найдено",
            "items": [
                {
                    "ingredient_name": "Спагетти",
                    "requested_quantity": "400 г",
                    "product_name": "Спагетти 450 г",
                    "product_url": "https://kuper.ru/product/1",
                    "package_count": 1,
                    "quality": "exact",
                    "warning": "",
                }
            ],
        }
        inspect.return_value = result
        assemble.return_value = result
        run = self.make_run()

        process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.COMPLETED)
        self.assertEqual(run.selected_attempt.status, CartAttempt.Status.EXACT)
        self.assertEqual(run.selected_attempt.matches.get().quality, CartItemMatch.MatchQuality.EXACT)

    @patch("recipes.carting.pipeline.assemble_store_cart")
    @patch("recipes.carting.pipeline.inspect_store_cart")
    def test_false_exact_is_downgraded_and_next_store_is_tried(self, inspect, assemble):
        substitute_result = {
            "status": "exact",
            "cart_url": "https://kuper.ru/cart",
            "summary": "Ошибочный exact",
            "items": [
                {
                    "ingredient_name": "Спагетти",
                    "requested_quantity": "400 г",
                    "product_name": "Лапша рисовая",
                    "product_url": "https://kuper.ru/product/2",
                    "package_count": 1,
                    "quality": "substitute",
                    "warning": "Другой вид лапши, может не подойти для рецепта.",
                }
            ],
        }
        inspect.side_effect = [
            substitute_result,
            {
                "status": "incomplete",
                "cart_url": "",
                "summary": "Не найдено",
                "items": [
                    {
                        "ingredient_name": "Спагетти",
                        "requested_quantity": "400 г",
                        "product_name": "",
                        "product_url": "",
                        "package_count": 0,
                        "quality": "missing",
                        "warning": "Нет подходящего товара.",
                    }
                ],
            },
        ]
        assemble.return_value = substitute_result
        run = self.make_run(["auchan", "perekrestok"])

        process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.REVIEW)
        self.assertEqual(run.attempts.count(), 2)
        first = run.attempts.get(store="auchan")
        self.assertEqual(first.status, CartAttempt.Status.SUBSTITUTIONS)
        self.assertEqual(run.selected_attempt, first)

    @patch("recipes.carting.pipeline.inspect_store_cart")
    def test_login_required_pauses_without_advancing_store(self, inspect):
        inspect.return_value = {
            "status": "login_required",
            "summary": "Войдите в аккаунт",
            "items": [],
        }
        run = self.make_run(["auchan", "perekrestok"])

        process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.LOGIN_REQUIRED)
        self.assertEqual(run.next_store_index, 0)

    @patch("recipes.carting.pipeline.inspect_store_cart")
    def test_captcha_pauses_same_store_for_manual_verification(self, inspect):
        inspect.return_value = {
            "status": "blocked",
            "summary": "Показана CAPTCHA",
            "items": [],
        }
        run = self.make_run(["auchan", "perekrestok", "lavka"])

        process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.LOGIN_REQUIRED)
        self.assertEqual(run.next_store_index, 0)
        self.assertEqual(list(run.attempts.values_list("store", flat=True)), ["auchan"])

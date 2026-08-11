from unittest.mock import patch
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from recipes.carting.pipeline import (
    claim_cart_run,
    expire_unconfirmed_cart_runs,
    process_cart_cleanup,
    process_cart_run,
)
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

    def test_confirm_keeps_user_cart(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.COMPLETED,
            store_priority=["auchan"],
            ingredient_snapshot=[],
            confirmation_deadline=timezone.now() + timedelta(hours=3),
        )
        attempt = CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.EXACT,
            cart_url="https://eda.yandex.ru/cart",
        )
        run.selected_attempt = attempt
        run.save(update_fields=["selected_attempt"])

        response = self.client.post(reverse("cart-confirm", args=[run.pk]))

        self.assertRedirects(response, reverse("cart-detail", args=[run.pk]))
        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CONFIRMED)
        self.assertIsNotNone(run.confirmed_at)
        self.assertIsNone(run.confirmation_deadline)

    def test_cancel_queues_cleanup_only_for_recorded_additions(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.COMPLETED,
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )
        attempt = CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.EXACT,
            result={
                "cart_cleared": False,
                "added_items": [
                    {"product_name": "Картофель", "package_count": 1}
                ],
            },
        )
        run.selected_attempt = attempt
        run.save(update_fields=["selected_attempt"])

        self.client.post(reverse("cart-cancel", args=[run.pk]))

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CLEANUP_PENDING)
        self.assertIsNotNone(run.cleanup_requested_at)


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
    def test_exact_cart_is_built_in_one_agent_call(self, assemble):
        result = {
            "status": "exact",
            "cart_url": "https://eda.yandex.ru/cart",
            "summary": "Всё найдено",
            "cart_cleared": False,
            "added_items": [
                {
                    "product_name": "Спагетти 450 г",
                    "product_url": "https://eda.yandex.ru/product/1",
                    "package_count": 1,
                }
            ],
            "items": [
                {
                    "ingredient_name": "Спагетти",
                    "requested_quantity": "400 г",
                    "product_name": "Спагетти 450 г",
                    "product_url": "https://eda.yandex.ru/product/1",
                    "package_count": 1,
                    "quality": "exact",
                    "warning": "",
                }
            ],
        }
        assemble.return_value = result
        run = self.make_run()

        process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.COMPLETED)
        self.assertIsNotNone(run.confirmation_deadline)
        self.assertEqual(run.selected_attempt.status, CartAttempt.Status.EXACT)
        self.assertEqual(run.selected_attempt.matches.get().quality, CartItemMatch.MatchQuality.EXACT)
        assemble.assert_called_once_with(run, "auchan")

    @patch("recipes.carting.pipeline.assemble_store_cart")
    def test_false_exact_with_substitute_stops_for_review(self, assemble):
        substitute_result = {
            "status": "exact",
            "cart_url": "https://eda.yandex.ru/cart",
            "summary": "Ошибочный exact",
            "cart_cleared": False,
            "added_items": [
                {"product_name": "Лапша рисовая", "package_count": 1}
            ],
            "items": [
                {
                    "ingredient_name": "Спагетти",
                    "requested_quantity": "400 г",
                    "product_name": "Лапша рисовая",
                    "product_url": "https://eda.yandex.ru/product/2",
                    "package_count": 1,
                    "quality": "substitute",
                    "warning": "Другой вид лапши, может не подойти для рецепта.",
                }
            ],
        }
        assemble.return_value = substitute_result
        run = self.make_run(["auchan", "perekrestok"])

        process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.REVIEW)
        self.assertEqual(run.attempts.count(), 1)
        first = run.attempts.get(store="auchan")
        self.assertEqual(first.status, CartAttempt.Status.SUBSTITUTIONS)
        self.assertEqual(run.selected_attempt, first)

    @patch("recipes.carting.pipeline.assemble_store_cart")
    def test_login_required_pauses_without_advancing_store(self, assemble):
        assemble.return_value = {
            "status": "login_required",
            "summary": "Войдите в аккаунт",
            "items": [],
        }
        run = self.make_run(["auchan", "perekrestok"])

        process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.LOGIN_REQUIRED)
        self.assertEqual(run.next_store_index, 0)

    @patch("recipes.carting.pipeline.assemble_store_cart")
    def test_captcha_pauses_same_store_for_manual_verification(self, assemble):
        assemble.return_value = {
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

    @patch("recipes.carting.pipeline.assemble_store_cart")
    def test_agent_cleared_incomplete_attempt_then_tries_next_store(self, assemble):
        run = self.make_run(["auchan", "perekrestok"])
        run.ingredient_snapshot = [
            {"name": name, "quantity": "1", "unit": "шт."}
            for name in ["Первый", "Второй", "Третий", "Четвёртый"]
        ]
        run.save(update_fields=["ingredient_snapshot"])
        incomplete = {
            "status": "incomplete",
            "summary": "Много позиций отсутствует, добавления удалены",
            "cart_cleared": True,
            "added_items": [{"product_name": "Первый товар", "package_count": 1}],
            "items": [
                {
                    "ingredient_name": name,
                    "requested_quantity": "1 шт.",
                    "product_name": "Первый товар" if index == 0 else "",
                    "package_count": 1 if index == 0 else 0,
                    "quality": "exact" if index == 0 else "missing",
                    "warning": "" if index == 0 else "Нет товара",
                }
                for index, name in enumerate(["Первый", "Второй", "Третий", "Четвёртый"])
            ],
        }
        exact = {
            "status": "exact",
            "cart_url": "https://eda.yandex.ru/cart",
            "cart_cleared": False,
            "added_items": [
                {"product_name": f"{name} товар", "package_count": 1}
                for name in ["Первый", "Второй", "Третий", "Четвёртый"]
            ],
            "items": [
                {
                    "ingredient_name": name,
                    "requested_quantity": "1 шт.",
                    "product_name": f"{name} товар",
                    "package_count": 1,
                    "quality": "exact",
                    "warning": "",
                }
                for name in ["Первый", "Второй", "Третий", "Четвёртый"]
            ],
        }
        assemble.side_effect = [incomplete, exact]

        process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.COMPLETED)
        self.assertEqual(run.selected_attempt.store, "perekrestok")
        self.assertEqual(assemble.call_count, 2)

    @patch("recipes.carting.pipeline.cleanup_store_cart")
    @patch("recipes.carting.pipeline.assemble_store_cart")
    def test_pipeline_falls_back_to_cleanup_when_agent_did_not(self, assemble, cleanup):
        run = self.make_run(["auchan"])
        run.ingredient_snapshot = [
            {"name": name, "quantity": "1", "unit": "шт."}
            for name in ["Первый", "Второй", "Третий", "Четвёртый"]
        ]
        run.save(update_fields=["ingredient_snapshot"])
        assemble.return_value = {
            "status": "incomplete",
            "cart_url": "https://eda.yandex.ru/cart",
            "cart_cleared": False,
            "added_items": [{"product_name": "Первый товар", "package_count": 1}],
            "items": [
                {
                    "ingredient_name": name,
                    "package_count": 1 if index < 2 else 0,
                    "product_name": f"{name} товар" if index < 2 else "",
                    "quality": "exact" if index < 2 else "missing",
                    "warning": "" if index < 2 else "Нет товара",
                }
                for index, name in enumerate(["Первый", "Второй", "Третий", "Четвёртый"])
            ],
        }
        cleanup.return_value = {"status": "cleared", "summary": "Очищено"}

        process_cart_run(run)

        run.refresh_from_db()
        attempt = run.attempts.get()
        self.assertEqual(run.status, CartRun.Status.REVIEW)
        self.assertTrue(attempt.result["cart_cleared"])
        self.assertFalse(attempt.cart_url)
        cleanup.assert_called_once()

    @patch("recipes.carting.pipeline.cleanup_store_cart")
    def test_expired_cart_is_cleaned_from_recorded_journal(self, cleanup):
        run = self.make_run()
        run.status = CartRun.Status.COMPLETED
        run.confirmation_deadline = timezone.now() - timedelta(minutes=1)
        run.save(update_fields=["status", "confirmation_deadline"])
        attempt = CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.EXACT,
            cart_url="https://eda.yandex.ru/cart",
            result={
                "cart_cleared": False,
                "added_items": [
                    {"product_name": "Спагетти", "package_count": 2}
                ],
            },
        )
        run.selected_attempt = attempt
        run.save(update_fields=["selected_attempt"])
        cleanup.return_value = {"status": "cleared", "summary": "Удалено 2 упаковки"}

        self.assertEqual(expire_unconfirmed_cart_runs(), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CLEANUP_PENDING)
        run.status = CartRun.Status.CLEANING
        run.save(update_fields=["status"])
        process_cart_cleanup(run)

        run.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)
        self.assertTrue(attempt.result["cart_cleared"])
        cleanup.assert_called_once_with(
            run,
            "auchan",
            [{"product_name": "Спагетти", "package_count": 2}],
            "https://eda.yandex.ru/cart",
        )

    def test_new_assembly_waits_for_unfinished_cleanup(self):
        old_run = self.make_run()
        old_run.status = CartRun.Status.LOGIN_REQUIRED
        old_run.cleanup_requested_at = timezone.now()
        old_run.save(update_fields=["status", "cleanup_requested_at"])
        CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PENDING,
            store_priority=["perekrestok"],
            ingredient_snapshot=self.snapshot,
        )

        self.assertIsNone(claim_cart_run())

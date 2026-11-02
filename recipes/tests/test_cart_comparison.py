from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from recipes.carting.client import CartAgentError
from recipes.carting.pipeline import (
    claim_cart_run,
    comparison_is_fresh,
    matches_total_price,
    process_cart_run,
)
from recipes.models import CartAttempt, CartItemMatch, CartRun, Recipe


PASTA_ID = "12345678-1234-1234-1234-123456789abc"
CREAM_ID = "abcdefab-1234-1234-1234-123456789abc"
LAVKA_PASTA_ID = "lavkapasta0000000000000001"


def eda_candidate(product_id, name, weight, price, store="asan_giper"):
    return {
        "product_id": product_id,
        "sku_id": product_id,
        "name": name,
        "weight": weight,
        "available": True,
        "in_stock": 20,
        "price": price,
        "product_url": (
            f"https://eda.yandex.ru/retail/{store}/product/{product_id}"
            "?placeSlug=ashan_w5r8t"
        ),
    }


ADAPTER_SETTINGS = {
    "CART_ADAPTER_BASE_URL": "https://adapter.example",
    "CART_ADAPTER_API_KEY": "adapter-key",
    "CART_COMPARE_TIMEOUT_SECONDS": 120,
}


@override_settings(**ADAPTER_SETTINGS)
class CartComparisonTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="cook")
        self.recipe = Recipe.objects.create(
            title="Паста", servings=2, created_by=self.user
        )
        self.snapshot = [
            {"name": "Спагетти", "quantity": "400", "unit": "г", "search_query": "спагетти"},
            {"name": "Сливки", "quantity": "200", "unit": "мл", "search_query": "сливки"},
        ]

    def make_run(self, stores=("auchan", "lavka", "magnit"), **fields):
        defaults = {
            "recipe": self.recipe,
            "requested_by": self.user,
            "servings": 2,
            "status": CartRun.Status.PROCESSING,
            "store_priority": list(stores),
            "ingredient_snapshot": self.snapshot,
            "comparison": True,
        }
        defaults.update(fields)
        return CartRun.objects.create(**defaults)

    def compare_response(self):
        return {
            "status": "ready",
            "elapsed_ms": 40_000,
            "stores": {
                "auchan": {
                    "status": "ready",
                    "selection_token": "signed-auchan",
                    "cart_url": "https://eda.yandex.ru/retail/asan_giper?placeSlug=ashan_w5r8t",
                    "elapsed_ms": 30_000,
                    "results": [
                        {
                            "index": 0,
                            "candidates": [
                                eda_candidate(PASTA_ID, "Спагетти 450 г", "450 g", "120.00")
                            ],
                        },
                        {
                            "index": 1,
                            "candidates": [
                                eda_candidate(CREAM_ID, "Сливки 200 мл", "200 мл", "99.50")
                            ],
                        },
                    ],
                },
                "lavka": {
                    "status": "ready",
                    "selection_token": "signed-lavka",
                    "cart_url": "https://lavka.yandex.ru/cart",
                    "results": [
                        {
                            "index": 0,
                            "candidates": [
                                {
                                    "product_id": LAVKA_PASTA_ID,
                                    "sku_id": LAVKA_PASTA_ID,
                                    "name": "Спагетти 500 г",
                                    "weight": "500 г",
                                    "price": "89",
                                    "product_url": f"https://lavka.yandex.ru/good/{LAVKA_PASTA_ID}",
                                }
                            ],
                        },
                        {"index": 1, "candidates": []},
                    ],
                },
                "magnit": {"status": "timeout", "summary": "Магазин не ответил вовремя."},
            },
        }

    @patch("recipes.carting.client._run_adapter_task")
    def test_comparison_searches_all_stores_at_once_without_cart_changes(self, adapter_task):
        adapter_task.return_value = self.compare_response()
        run = self.make_run()

        process_cart_run(run)

        adapter_task.assert_called_once()
        path, payload = adapter_task.call_args.args
        self.assertEqual(path, "/v1/compare")
        self.assertEqual(payload["stores"], ["auchan", "lavka", "magnit"])
        self.assertEqual(payload["budget_ms"], 120_000)
        self.assertEqual(adapter_task.call_args.kwargs["timeout"], 180)
        self.assertFalse(adapter_task.call_args.kwargs["mutation_possible"])
        for store in ("auchan", "lavka", "magnit"):
            self.assertRegex(
                payload["operation_ids"][store],
                rf"^cart-run-{run.pk}-[0-9]{{20}}-{store}$",
            )

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CHOOSING)
        self.assertIsNone(run.browser_operation_started_at)
        self.assertIsNone(run.selected_attempt)
        auchan = run.attempts.get(store="auchan")
        self.assertEqual(auchan.status, CartAttempt.Status.EXACT)
        self.assertEqual(auchan.cart_url, "")
        self.assertEqual(auchan.total_price, Decimal("219.50"))
        self.assertEqual(
            list(auchan.matches.values_list("price", flat=True)),
            [Decimal("120.00"), Decimal("99.50")],
        )
        self.assertEqual(auchan.result["comparison"]["selection_token"], "signed-auchan")
        self.assertTrue(comparison_is_fresh(auchan))
        lavka = run.attempts.get(store="lavka")
        self.assertEqual(lavka.status, CartAttempt.Status.INCOMPLETE)
        self.assertEqual(lavka.total_price, Decimal("89.00"))
        self.assertEqual(lavka.result["counts"]["missing"], 1)
        magnit = run.attempts.get(store="magnit")
        self.assertEqual(magnit.status, CartAttempt.Status.FAILED)
        self.assertEqual(magnit.summary, "Магазин не ответил вовремя.")
        self.assertFalse(magnit.matches.exists())

    @patch("recipes.carting.client._run_adapter_task")
    def test_comparison_page_offers_each_found_store(self, adapter_task):
        adapter_task.return_value = self.compare_response()
        run = self.make_run()
        process_cart_run(run)
        self.client.force_login(self.user)

        response = self.client.get(reverse("cart-detail", args=[run.pk]))

        self.assertContains(response, "Выберите магазин")
        self.assertContains(response, "Собрать в «Ашан»")
        self.assertContains(response, "Собрать в «Яндекс Лавка»")
        self.assertContains(response, "Дешевле всего")
        self.assertContains(response, "219,5")
        self.assertContains(response, "без 1 поз.")
        self.assertContains(response, "Магазин не ответил вовремя.")
        rows = response.context["comparison_rows"]
        self.assertEqual([row["store"] for row in rows], ["auchan", "lavka", "magnit"])
        self.assertTrue(rows[0]["best"])

    @patch("recipes.carting.client._run_adapter_task")
    def test_transport_failure_of_a_read_only_comparison_is_retryable(self, adapter_task):
        adapter_task.side_effect = CartAgentError(
            "Адаптер корзины недоступен.",
            mutation_possible=True,
            operation_uncertain=True,
        )
        run = self.make_run()

        process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.FAILED)
        self.assertIsNone(run.browser_operation_started_at)
        self.assertEqual(
            set(run.attempts.values_list("status", flat=True)),
            {CartAttempt.Status.FAILED},
        )

    @patch("recipes.carting.client._run_adapter_task")
    def test_all_stores_asking_for_login_pause_for_login(self, adapter_task):
        adapter_task.return_value = {
            "status": "login_required",
            "summary": "Нужно войти в Яндекс Еду.",
        }
        run = self.make_run()

        process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.LOGIN_REQUIRED)

    @patch("recipes.carting.client._run_adapter_task")
    def test_choosing_a_store_applies_exactly_the_compared_products(self, adapter_task):
        adapter_task.return_value = self.compare_response()
        run = self.make_run()
        process_cart_run(run)
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("cart-choose", args=[run.pk]), {"store": "lavka"}
        )

        self.assertRedirects(response, reverse("cart-detail", args=[run.pk]))
        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.PENDING)
        self.assertEqual(run.chosen_store, "lavka")
        self.assertEqual(run.store_priority, ["lavka"])
        claimed = claim_cart_run()
        self.assertEqual(claimed.pk, run.pk)

        adapter_task.reset_mock()
        adapter_task.side_effect = None
        adapter_task.return_value = {
            "status": "applied",
            "cart_url": "https://lavka.yandex.ru/cart",
            "elapsed_ms": 900,
            "additions": [{"product_id": LAVKA_PASTA_ID, "added_quantity": 1}],
            "cleanup_token": "signed-cleanup",
        }
        process_cart_run(claimed)

        # No new search: the signed comparison is applied as reviewed, even
        # though one ingredient is missing in this store.
        adapter_task.assert_called_once()
        path, payload = adapter_task.call_args.args
        self.assertEqual(path, "/v1/apply")
        self.assertEqual(payload["store"], "lavka")
        self.assertEqual(payload["selection_token"], "signed-lavka")
        self.assertEqual(
            payload["items"],
            [{"product_id": LAVKA_PASTA_ID, "sku_id": LAVKA_PASTA_ID, "package_count": 1}],
        )
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, CartRun.Status.REVIEW)
        self.assertEqual(claimed.selected_attempt.store, "lavka")
        self.assertEqual(claimed.selected_attempt.cart_url, "https://lavka.yandex.ru/cart")
        self.assertEqual(claimed.selected_attempt.total_price, Decimal("89.00"))
        self.assertIsNotNone(claimed.confirmation_deadline)
        # The other stores keep their comparison results.
        self.assertEqual(
            claimed.attempts.get(store="auchan").status, CartAttempt.Status.EXACT
        )
        detail = self.client.get(reverse("cart-detail", args=[run.pk]))
        self.assertContains(detail, "Выбран")
        self.assertContains(detail, "Проверить магазины заново")

    @patch("recipes.carting.client._run_adapter_task")
    def test_stale_comparison_cannot_be_applied(self, adapter_task):
        adapter_task.return_value = self.compare_response()
        run = self.make_run()
        process_cart_run(run)
        attempt = run.attempts.get(store="auchan")
        attempt.result["comparison"]["searched_at"] = (
            timezone.now() - timedelta(hours=2)
        ).isoformat()
        attempt.save(update_fields=["result"])
        self.client.force_login(self.user)

        self.client.post(reverse("cart-choose", args=[run.pk]), {"store": "auchan"})

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CHOOSING)
        self.assertEqual(run.chosen_store, "")

    @patch("recipes.carting.client._run_adapter_task")
    def test_failed_store_cannot_be_chosen(self, adapter_task):
        adapter_task.return_value = self.compare_response()
        run = self.make_run()
        process_cart_run(run)
        self.client.force_login(self.user)

        self.client.post(reverse("cart-choose", args=[run.pk]), {"store": "magnit"})

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CHOOSING)

    def test_compare_again_starts_a_new_comparison(self):
        run = self.make_run(status=CartRun.Status.CHOOSING)
        self.client.force_login(self.user)

        response = self.client.post(reverse("cart-compare-again", args=[run.pk]))

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)
        new_run = CartRun.objects.exclude(pk=run.pk).get()
        self.assertRedirects(response, reverse("cart-detail", args=[new_run.pk]))
        self.assertTrue(new_run.comparison)
        self.assertEqual(new_run.status, CartRun.Status.PENDING)
        self.assertEqual(new_run.ingredient_snapshot, self.snapshot)

    @patch("recipes.carting.client._run_adapter_task")
    def test_stop_after_choosing_judges_the_chosen_store_only(self, adapter_task):
        adapter_task.return_value = self.compare_response()
        run = self.make_run()
        process_cart_run(run)
        chosen = run.attempts.get(store="auchan")
        chosen.result = {**chosen.result, "mutation_unknown": True}
        chosen.save(update_fields=["result"])
        CartRun.objects.filter(pk=run.pk).update(
            status=CartRun.Status.PENDING,
            chosen_store="auchan",
            store_priority=["auchan"],
        )
        self.client.force_login(self.user)

        self.client.post(reverse("cart-stop", args=[run.pk]))

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)
        self.assertEqual(run.selected_attempt.store, "auchan")
        # Additions of the chosen store may remain, so the run warns.
        self.assertIsNone(run.cleaned_at)

    def test_choosing_run_can_be_cancelled(self):
        run = self.make_run(status=CartRun.Status.CHOOSING)
        self.client.force_login(self.user)

        self.client.post(reverse("cart-stop", args=[run.pk]))

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)
        self.assertIsNotNone(run.cleaned_at)


class TotalPriceTests(TestCase):
    def test_total_counts_packages_and_skips_missing_items(self):
        total, complete = matches_total_price(
            [
                {"quality": "exact", "price": "120.00", "package_count": 2},
                {"quality": "substitute", "price": "99.5", "package_count": 1},
                {"quality": "missing", "price": "", "package_count": 0},
            ]
        )
        self.assertEqual(total, Decimal("339.50"))
        self.assertTrue(complete)

    def test_unknown_price_marks_total_as_partial(self):
        total, complete = matches_total_price(
            [
                {"quality": "exact", "price": "120", "package_count": 1},
                {"quality": "exact", "price": None, "package_count": 1},
            ]
        )
        self.assertEqual(total, Decimal("120.00"))
        self.assertFalse(complete)

    def test_nothing_found_has_no_total(self):
        self.assertEqual(
            matches_total_price([{"quality": "missing"}]),
            (None, True),
        )

    def test_match_line_total(self):
        match = CartItemMatch(price=Decimal("10.50"), package_count=3)
        self.assertEqual(match.line_total, Decimal("31.50"))

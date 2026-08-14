from datetime import timedelta
from unittest.mock import patch

import httpx
from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import DatabaseError
from django.db.models.deletion import ProtectedError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from recipes.carting.pipeline import (
    _release_browser_operation,
    claim_cart_run,
    expire_unconfirmed_cart_runs,
    finish_requested_cart_stop,
    process_cart_cleanup,
    process_cart_run,
)
from recipes.admin import BrowserLoginSessionAdmin
from recipes.carting.browser_login import (
    BrowserLoginError,
    BrowserLoginSessionNotFound,
    issue_access,
    recover_scope,
)
from recipes.carting.client import (
    ASSEMBLE_PROMPT,
    STORE_INSTRUCTIONS,
    CartAgentError,
    assemble_store_cart,
    cart_browser_session_key,
    cleanup_store_cart,
    _run_adapter_task,
    run_store_cart_task,
)
from recipes.carting.coordination import browser_login_session_blocks_worker
from recipes.carting.matching import choose_product, enforce_aggregate_stock
from recipes.management.commands.run_cart_worker import Command as CartWorkerCommand
from recipes.models import (
    BrowserLoginSession,
    CartAttempt,
    CartItemMatch,
    CartRun,
    Recipe,
    RecipeIngredient,
    StorePreference,
)
from recipes.services import get_store_preferences, select_store


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

    def test_default_store_selection_is_auchan(self):
        preferences = get_store_preferences(self.user)
        self.assertEqual(
            [item.store for item in preferences],
            ["auchan", "perekrestok", "pyaterochka", "magnit", "lavka"],
        )
        self.assertEqual(
            [item.store for item in preferences if item.enabled],
            [StorePreference.Store.AUCHAN],
        )

    def test_start_cart_snapshots_selected_scaled_ingredients(self):
        response = self.client.post(
            reverse("cart-start", args=[self.recipe.slug]),
            {
                "servings": 4,
                "store": StorePreference.Store.PEREKRESTOK,
                "ingredients": [str(self.potato.pk)],
            },
        )
        run = CartRun.objects.get()
        self.assertRedirects(response, reverse("cart-detail", args=[run.pk]))
        self.assertEqual(run.store_priority, [StorePreference.Store.PEREKRESTOK])
        self.assertEqual(run.ingredient_snapshot[0]["name"], "Картофель")
        self.assertEqual(run.ingredient_snapshot[0]["quantity"], "800")
        detail = self.client.get(reverse("cart-detail", args=[run.pk]))
        self.assertNotContains(detail, "Соль")
        self.assertContains(detail, "В очереди")

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

    def test_login_action_is_scoped_to_requesting_user(self):
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
            f'{reverse("yandex-connection")}?run={run.pk}',
        )
        self.assertNotContains(response, "cart-browser-login-pi.sh")

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
    )
    def test_yandex_connection_has_a_dedicated_link_button(self):
        response = self.client.get(reverse("yandex-connection"))

        self.assertContains(response, "Привязать Яндекс Еду")
        self.assertContains(response, reverse("browser-login-start"))
        self.assertContains(response, "Аккаунт ещё не привязан")

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
    )
    def test_yandex_connection_remembers_a_completed_profile(self):
        BrowserLoginSession.objects.create(
            user=self.user,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.COMPLETED,
            expires_at=timezone.now(),
            finished_at=timezone.now(),
        )

        response = self.client.get(reverse("yandex-connection"))

        self.assertContains(response, "Аккаунт привязан")
        self.assertContains(response, "Обновить привязку")

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
    )
    def test_yandex_connection_attaches_an_open_session_through_start_endpoint(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["lavka"],
            ingredient_snapshot=[],
        )
        login_session = BrowserLoginSession.objects.create(
            user=self.user,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        response = self.client.get(f'{reverse("yandex-connection")}?run={run.pk}')

        self.assertContains(
            response,
            f'action="{reverse("cart-browser-login-start", args=[run.pk])}"',
        )
        self.assertNotContains(
            response,
            f'href="{reverse("browser-login", args=[login_session.pk])}"',
        )

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
    )
    def test_yandex_connection_points_a_conflicting_session_to_its_own_run(self):
        active_run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["lavka"],
            ingredient_snapshot=[],
        )
        requested_run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )
        BrowserLoginSession.objects.create(
            user=self.user,
            run=active_run,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        response = self.client.get(
            f'{reverse("yandex-connection")}?run={requested_run.pk}'
        )

        self.assertContains(response, "Окно занято другой сборкой")
        self.assertContains(response, reverse("cart-detail", args=[active_run.pk]))
        self.assertNotContains(
            response,
            f'action="{reverse("cart-browser-login-start", args=[requested_run.pk])}"',
        )

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
        CART_BROWSER_LOGIN_MINUTES=15,
    )
    @patch("recipes.views.start_browser_login_session")
    def test_browser_login_starts_user_profile(self, start_session):
        start_session.return_value = "remote-session-id-1234567890"
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["lavka"],
            ingredient_snapshot=[],
        )

        response = self.client.post(reverse("cart-browser-login-start", args=[run.pk]))

        login_session = BrowserLoginSession.objects.get()
        self.assertRedirects(
            response,
            reverse("browser-login", args=[login_session.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(login_session.user, self.user)
        self.assertEqual(login_session.run, run)
        self.assertEqual(login_session.status, BrowserLoginSession.Status.ACTIVE)
        start_session.assert_called_once_with(
            cart_browser_session_key(self.user.pk),
            15,
            login_session.remote_session_id,
        )

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
        CART_BROWSER_LOGIN_MINUTES=15,
    )
    @patch("recipes.views.start_browser_login_session")
    def test_uncertain_cart_operation_blocks_manual_browser_start(self, start_session):
        CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.MANUAL_CHECK,
            browser_operation_started_at=timezone.now(),
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )

        response = self.client.post(reverse("browser-login-start"))

        self.assertRedirects(response, reverse("yandex-connection"))
        self.assertFalse(BrowserLoginSession.objects.exists())
        start_session.assert_not_called()

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
        CART_BROWSER_LOGIN_MINUTES=15,
    )
    @patch("recipes.views.stop_browser_login_session")
    @patch("recipes.views.start_browser_login_session")
    def test_failed_start_releases_worker_only_after_confirmed_cleanup(
        self,
        start_session,
        stop_session,
    ):
        start_session.side_effect = BrowserLoginError("start failed")
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["lavka"],
            ingredient_snapshot=[],
        )

        self.client.post(reverse("cart-browser-login-start", args=[run.pk]))

        login_session = BrowserLoginSession.objects.get()
        self.assertEqual(login_session.status, BrowserLoginSession.Status.FAILED)
        stop_session.assert_called_once_with(login_session.remote_session_id)

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
        CART_BROWSER_LOGIN_MINUTES=15,
    )
    @patch("recipes.views.stop_browser_login_session")
    @patch("recipes.views.start_browser_login_session")
    def test_uncertain_start_cleanup_keeps_worker_blocked(
        self,
        start_session,
        stop_session,
    ):
        start_session.side_effect = BrowserLoginError("start connection lost")
        stop_session.side_effect = BrowserLoginError("controller unavailable")
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["lavka"],
            ingredient_snapshot=[],
        )

        self.client.post(reverse("cart-browser-login-start", args=[run.pk]))

        login_session = BrowserLoginSession.objects.get()
        self.assertEqual(login_session.status, BrowserLoginSession.Status.STARTING)
        self.assertIn("start connection lost", login_session.error)
        pending_run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PENDING,
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )
        self.assertIsNone(claim_cart_run())
        pending_run.refresh_from_db()
        self.assertEqual(pending_run.status, CartRun.Status.PENDING)

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
        CART_BROWSER_LOGIN_MINUTES=15,
    )
    @patch("recipes.views.stop_browser_login_session")
    @patch("recipes.views.start_browser_login_session")
    def test_finalize_failure_compensates_remote_session(
        self,
        start_session,
        stop_session,
    ):
        original_save = BrowserLoginSession.save

        def fail_active_save(instance, *args, **kwargs):
            if (
                instance.status == BrowserLoginSession.Status.ACTIVE
                and kwargs.get("update_fields") == ["status"]
            ):
                raise DatabaseError("commit failed")
            return original_save(instance, *args, **kwargs)

        with patch.object(BrowserLoginSession, "save", new=fail_active_save):
            self.client.post(reverse("browser-login-start"))

        login_session = BrowserLoginSession.objects.get()
        self.assertEqual(login_session.status, BrowserLoginSession.Status.FAILED)
        stop_session.assert_called_once_with(login_session.remote_session_id)

    @patch("recipes.views.issue_browser_login_access")
    def test_browser_login_page_embeds_one_time_access_path(self, issue_access):
        issue_access.return_value = "/browser-login/access/abcdefghijklmnopqrstuvwxyz123456"
        login_session = BrowserLoginSession.objects.create(
            user=self.user,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        response = self.client.get(reverse("browser-login", args=[login_session.pk]))

        self.assertContains(response, issue_access.return_value)
        self.assertContains(response, "Сохранить сессию и продолжить")
        issue_access.assert_called_once_with(login_session.remote_session_id)

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
        CART_BROWSER_LOGIN_MINUTES=15,
    )
    @patch("recipes.carting.coordination.stop_session")
    @patch("recipes.views.issue_browser_login_access")
    def test_missing_remote_login_is_reconciled_and_can_restart(
        self,
        issue_login_access,
        stop_session,
    ):
        issue_login_access.side_effect = BrowserLoginSessionNotFound("missing")
        stale_session = BrowserLoginSession.objects.create(
            user=self.user,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        response = self.client.get(reverse("browser-login", args=[stale_session.pk]))

        self.assertRedirects(response, reverse("yandex-connection"))
        stale_session.refresh_from_db()
        self.assertEqual(stale_session.status, BrowserLoginSession.Status.FAILED)
        stop_session.assert_called_once_with(stale_session.remote_session_id)

        with patch("recipes.views.start_browser_login_session"):
            restart = self.client.post(reverse("browser-login-start"))

        replacement = BrowserLoginSession.objects.exclude(pk=stale_session.pk).get()
        self.assertRedirects(
            restart,
            reverse("browser-login", args=[replacement.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(replacement.status, BrowserLoginSession.Status.ACTIVE)

    @patch("recipes.carting.coordination.stop_session")
    @patch("recipes.views.issue_browser_login_access")
    def test_missing_remote_login_stays_blocking_if_recheck_fails(
        self,
        issue_login_access,
        stop_session,
    ):
        issue_login_access.side_effect = BrowserLoginSessionNotFound("missing")
        stop_session.side_effect = BrowserLoginError("controller unavailable")
        stale_session = BrowserLoginSession.objects.create(
            user=self.user,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        self.client.get(reverse("browser-login", args=[stale_session.pk]))

        stale_session.refresh_from_db()
        self.assertEqual(stale_session.status, BrowserLoginSession.Status.ACTIVE)

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
    )
    @patch("recipes.carting.browser_login.httpx.Client")
    def test_access_404_has_a_distinct_missing_session_error(self, client_class):
        response = client_class.return_value.__enter__.return_value.request.return_value
        response.status_code = 404

        with self.assertRaises(BrowserLoginSessionNotFound):
            issue_access("remote-session-id-1234567890")

    def test_active_browser_session_cannot_be_deleted_through_admin_or_user(self):
        BrowserLoginSession.objects.create(
            user=self.user,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        session_admin = BrowserLoginSessionAdmin(BrowserLoginSession, AdminSite())

        self.assertFalse(session_admin.has_delete_permission(request=None))
        with self.assertRaises(ProtectedError):
            self.user.delete()

    @patch("recipes.views.stop_browser_login_session")
    def test_browser_login_completion_saves_and_resumes_run(self, stop_session):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["lavka"],
            ingredient_snapshot=[],
        )
        login_session = BrowserLoginSession.objects.create(
            user=self.user,
            run=run,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        def observe_committed_transition(_remote_session_id):
            login_session.refresh_from_db()
            self.assertEqual(
                login_session.status,
                BrowserLoginSession.Status.COMPLETING,
            )
            self.assertIsNotNone(login_session.transition_started_at)
            self.assertTrue(browser_login_session_blocks_worker())

        stop_session.side_effect = observe_committed_transition

        response = self.client.post(
            reverse("browser-login-complete", args=[login_session.pk])
        )

        self.assertRedirects(response, reverse("cart-detail", args=[run.pk]))
        run.refresh_from_db()
        login_session.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.PENDING)
        self.assertEqual(login_session.status, BrowserLoginSession.Status.COMPLETED)
        stop_session.assert_called_once_with(login_session.remote_session_id)

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
    )
    def test_existing_unbound_login_session_attaches_to_requested_run(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["lavka"],
            ingredient_snapshot=[],
        )
        login_session = BrowserLoginSession.objects.create(
            user=self.user,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        response = self.client.post(reverse("cart-browser-login-start", args=[run.pk]))

        self.assertRedirects(
            response,
            reverse("browser-login", args=[login_session.pk]),
            fetch_redirect_response=False,
        )
        login_session.refresh_from_db()
        self.assertEqual(login_session.run, run)

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
    )
    def test_existing_login_session_rejects_a_different_run(self):
        first_run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["lavka"],
            ingredient_snapshot=[],
        )
        second_run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )
        login_session = BrowserLoginSession.objects.create(
            user=self.user,
            run=first_run,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        response = self.client.post(
            reverse("cart-browser-login-start", args=[second_run.pk]),
            follow=True,
        )

        self.assertContains(response, "Окно входа уже связано с другой сборкой")
        login_session.refresh_from_db()
        self.assertEqual(login_session.run, first_run)

    def test_store_preferences_select_exactly_one_store(self):
        get_store_preferences(self.user)
        response = self.client.post(
            reverse("store-preferences"),
            {"store": StorePreference.Store.LAVKA},
        )
        self.assertRedirects(response, reverse("store-preferences"))
        enabled = list(
            self.user.store_preferences.filter(enabled=True).values_list("store", flat=True)
        )
        self.assertEqual(enabled, ["lavka"])

    def test_reselecting_current_store_keeps_exactly_one_store_enabled(self):
        get_store_preferences(self.user)

        selected = select_store(self.user, StorePreference.Store.AUCHAN)

        self.assertTrue(selected.enabled)
        self.assertEqual(
            list(
                self.user.store_preferences.filter(enabled=True).values_list(
                    "store", flat=True
                )
            ),
            [StorePreference.Store.AUCHAN],
        )

    def test_store_selection_returns_to_shopping_list(self):
        get_store_preferences(self.user)
        return_url = reverse("shopping-list", args=[self.recipe.slug])
        response = self.client.post(
            reverse("store-preferences"),
            {
                "store": StorePreference.Store.MAGNIT,
                "next": return_url,
            },
        )
        self.assertRedirects(response, return_url)
        preferences = get_store_preferences(self.user)
        self.assertEqual(
            [item.store for item in preferences if item.enabled],
            [StorePreference.Store.MAGNIT],
        )

    def test_cart_detail_shows_per_item_status_instead_of_checked_stores(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.REVIEW,
            store_priority=["auchan"],
            ingredient_snapshot=[
                {"name": "Картофель", "quantity": "400", "unit": "г"},
                {"name": "Соль", "quantity": "5", "unit": "г"},
            ],
        )
        attempt = CartAttempt.objects.create(
            run=run, store="auchan", status=CartAttempt.Status.SUBSTITUTIONS
        )
        run.selected_attempt = attempt
        run.save(update_fields=["selected_attempt"])
        CartItemMatch.objects.create(
            attempt=attempt,
            ingredient_name="Картофель",
            product_name="Картофель молодой",
            quality=CartItemMatch.MatchQuality.SUBSTITUTE,
        )

        response = self.client.get(reverse("cart-detail", args=[run.pk]))

        self.assertContains(response, "Найдена альтернатива")
        self.assertContains(response, "Ничего не найдено")
        self.assertNotContains(response, "В очереди")
        self.assertNotContains(response, "Проверенные магазины")

    def test_cart_detail_marks_items_unchecked_after_failed_attempt(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["auchan"],
            ingredient_snapshot=[
                {"name": "Картофель", "quantity": "400", "unit": "г"},
            ],
        )
        attempt = CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.BLOCKED,
        )
        run.selected_attempt = attempt
        run.save(update_fields=["selected_attempt"])

        response = self.client.get(reverse("cart-detail", args=[run.pk]))

        self.assertContains(response, "Не проверено")
        self.assertNotContains(response, "Ничего не найдено")

    def test_failed_cart_offers_retry_without_login_actions(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.FAILED,
            store_priority=["magnit"],
            ingredient_snapshot=[],
            error="API магазина отклонил товар.",
        )

        response = self.client.get(reverse("cart-detail", args=[run.pk]))

        self.assertContains(response, "Повторить сборку")
        self.assertNotContains(response, "Войти в Яндекс Еду")
        self.assertNotContains(response, "Уже вошли — продолжить")

    def test_cart_detail_keeps_separate_matches_for_duplicate_ingredient_names(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.REVIEW,
            store_priority=["auchan"],
            ingredient_snapshot=[
                {"name": "Масло", "quantity": "20", "unit": "г"},
                {"name": "Масло", "quantity": "10", "unit": "г"},
            ],
        )
        attempt = CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.SUBSTITUTIONS,
        )
        run.selected_attempt = attempt
        run.save(update_fields=["selected_attempt"])
        CartItemMatch.objects.create(
            attempt=attempt,
            ingredient_name="Масло",
            product_name="Масло первое",
            quality=CartItemMatch.MatchQuality.EXACT,
            order=0,
        )
        CartItemMatch.objects.create(
            attempt=attempt,
            ingredient_name="Масло",
            product_name="Масло второе",
            quality=CartItemMatch.MatchQuality.SUBSTITUTE,
            order=1,
        )

        response = self.client.get(reverse("cart-detail", args=[run.pk]))

        statuses = response.context["cart_item_statuses"]
        self.assertEqual(
            [item["match"].product_name for item in statuses],
            ["Масло первое", "Масло второе"],
        )
        self.assertEqual(
            [item["quality"] for item in statuses],
            ["exact", "substitute"],
        )

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

    def test_retry_after_unavailable_delivery_rechecks_selected_store(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.FAILED,
            store_priority=["auchan"],
            ingredient_snapshot=[],
            next_store_index=1,
        )
        CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.FAILED,
            result={"reason": "store_unavailable"},
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

    def test_manual_check_resolution_cancels_unknown_assembly(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.MANUAL_CHECK,
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )
        attempt = CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.FAILED,
            result={"mutation_unknown": True},
        )
        run.selected_attempt = attempt
        run.save(update_fields=["selected_attempt"])

        response = self.client.post(reverse("cart-manual-resolved", args=[run.pk]))

        self.assertRedirects(response, reverse("cart-detail", args=[run.pk]))
        run.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)
        self.assertEqual(run.selected_attempt, attempt)
        self.assertIsNotNone(run.cleaned_at)
        self.assertFalse(attempt.result["mutation_unknown"])
        self.assertTrue(attempt.result["cart_cleared"])

    def test_manual_check_resolution_finishes_uncertain_cleanup(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.MANUAL_CHECK,
            store_priority=["auchan"],
            ingredient_snapshot=[],
            cleanup_requested_at=timezone.now(),
        )
        attempt = CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.EXACT,
            result={"mutation_unknown": True},
        )
        run.selected_attempt = attempt
        run.save(update_fields=["selected_attempt"])

        self.client.post(reverse("cart-manual-resolved", args=[run.pk]))

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)
        self.assertIsNotNone(run.cleaned_at)

    def test_manual_check_without_attempt_can_be_resolved(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.MANUAL_CHECK,
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )

        response = self.client.post(reverse("cart-manual-resolved", args=[run.pk]))

        self.assertRedirects(response, reverse("cart-detail", args=[run.pk]))
        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)
        self.assertIsNotNone(run.cleaned_at)

    def test_pending_cart_job_can_be_stopped_immediately(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PENDING,
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )

        response = self.client.post(reverse("cart-stop", args=[run.pk]))

        self.assertRedirects(response, reverse("cart-detail", args=[run.pk]))
        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)
        self.assertIsNotNone(run.cancellation_requested_at)
        self.assertIsNotNone(run.cleaned_at)

    def test_manual_check_can_be_stopped_without_blocking_future_carts(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.MANUAL_CHECK,
            store_priority=["auchan"],
            ingredient_snapshot=[],
            cleanup_requested_at=timezone.now(),
        )
        attempt = CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.FAILED,
            result={"mutation_unknown": True},
        )
        run.selected_attempt = attempt
        run.save(update_fields=["selected_attempt"])

        self.client.post(reverse("cart-stop", args=[run.pk]))

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)
        self.assertIsNone(run.cleanup_requested_at)
        self.assertIsNone(run.cleaned_at)
        self.assertIn("могли остаться товары", run.error)

    def test_processing_cart_stop_is_finished_cooperatively(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PROCESSING,
            browser_operation_started_at=timezone.now(),
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )

        self.client.post(reverse("cart-stop", args=[run.pk]))

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.PROCESSING)
        self.assertIsNotNone(run.cancellation_requested_at)
        self.assertTrue(finish_requested_cart_stop(run))
        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)

    def test_pending_stop_can_be_rechecked_from_cart_page(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PROCESSING,
            browser_operation_started_at=timezone.now(),
            cancellation_requested_at=timezone.now(),
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )

        response = self.client.get(reverse("cart-detail", args=[run.pk]))

        self.assertContains(response, "Проверить остановку ещё раз")
        self.assertNotContains(response, "Остановка запрошена</button>")

    def test_processing_without_a_browser_reservation_stops_immediately(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PROCESSING,
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )

        self.client.post(reverse("cart-stop", args=[run.pk]))

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)
        self.assertIsNotNone(run.cleaned_at)

    def test_stop_uses_latest_attempt_when_selected_attempt_is_not_set(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PROCESSING,
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )
        attempt = CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.EXACT,
            result={
                "added_items": [
                    {
                        "product_name": "Картофель",
                        "product_url": "https://eda.yandex.ru/product/potato-12345678",
                        "package_count": 1,
                    }
                ]
            },
        )

        self.client.post(reverse("cart-stop", args=[run.pk]))

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)
        self.assertEqual(run.selected_attempt, attempt)
        self.assertIsNone(run.cleaned_at)
        self.assertIn("могли остаться товары", run.error)

    @override_settings(
        CART_AI_TIMEOUT_SECONDS=900,
        CART_ADAPTER_TIMEOUT_SECONDS=210,
    )
    @patch("recipes.views.recover_browser_scope")
    def test_stale_browser_reservation_requires_confirmed_remote_recovery(
        self,
        recover_scope,
    ):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PROCESSING,
            browser_operation_started_at=timezone.now() - timedelta(minutes=17),
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )

        self.client.post(reverse("cart-stop", args=[run.pk]))

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.MANUAL_CHECK)
        self.assertIsNotNone(run.browser_operation_started_at)
        self.assertIsNone(run.cleaned_at)
        self.assertIn("не подтверждено", run.error)

        self.client.post(reverse("cart-manual-resolved", args=[run.pk]))

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)
        self.assertIsNone(run.browser_operation_started_at)
        self.assertIsNotNone(run.cleaned_at)
        recover_scope.assert_called_once_with(cart_browser_session_key(self.user.pk))

    @patch("recipes.views.recover_browser_scope")
    def test_failed_remote_recovery_keeps_global_reservation(self, recover_scope):
        recover_scope.side_effect = BrowserLoginError("controller unavailable")
        started_at = timezone.now() - timedelta(minutes=17)
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.MANUAL_CHECK,
            browser_operation_started_at=started_at,
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )

        response = self.client.post(
            reverse("cart-manual-resolved", args=[run.pk]),
            follow=True,
        )

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.MANUAL_CHECK)
        self.assertEqual(run.browser_operation_started_at, started_at)
        self.assertContains(response, "Не удалось подтвердить остановку")

    @patch("recipes.views.stop_browser_login_session")
    def test_stopping_login_required_closes_its_browser_session(self, stop_session):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )
        login_session = BrowserLoginSession.objects.create(
            user=self.user,
            run=run,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        def observe_committed_transition(_remote_session_id):
            login_session.refresh_from_db()
            run.refresh_from_db()
            self.assertEqual(
                login_session.status,
                BrowserLoginSession.Status.STOPPING,
            )
            self.assertIsNotNone(login_session.transition_started_at)
            self.assertIsNotNone(run.cancellation_requested_at)
            self.assertTrue(browser_login_session_blocks_worker())

        stop_session.side_effect = observe_committed_transition

        response = self.client.post(reverse("cart-stop", args=[run.pk]))

        self.assertRedirects(response, reverse("cart-detail", args=[run.pk]))
        run.refresh_from_db()
        login_session.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)
        self.assertEqual(login_session.status, BrowserLoginSession.Status.FAILED)
        self.assertFalse(browser_login_session_blocks_worker())
        stop_session.assert_called_once_with(login_session.remote_session_id)

        stale_completion = self.client.post(
            reverse("browser-login-complete", args=[login_session.pk])
        )
        self.assertEqual(stale_completion.status_code, 404)
        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)

    @patch("recipes.views.stop_browser_login_session")
    def test_failed_browser_close_does_not_partially_stop_run(self, stop_session):
        stop_session.side_effect = BrowserLoginError("controller unavailable")
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["auchan"],
            ingredient_snapshot=[],
        )
        login_session = BrowserLoginSession.objects.create(
            user=self.user,
            run=run,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        self.client.post(reverse("cart-stop", args=[run.pk]))

        run.refresh_from_db()
        login_session.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.LOGIN_REQUIRED)
        self.assertIsNone(run.cancellation_requested_at)
        self.assertEqual(login_session.status, BrowserLoginSession.Status.ACTIVE)
        self.assertIsNone(login_session.transition_started_at)


class CartProductMatchingTests(SimpleTestCase):
    def candidate(self, **overrides):
        candidate = {
            "product_id": "product-12345678",
            "sku_id": "product-12345678",
            "name": "Молоко 3,2% 900 мл",
            "weight": "900 ml",
            "available": True,
            "in_stock": 10,
            "product_url": (
                "https://eda.yandex.ru/retail/shop/product/product-12345678"
                "?placeSlug=shop-nearby"
            ),
        }
        candidate.update(overrides)
        return candidate

    def test_calculates_package_count_from_metric_units(self):
        match = choose_product(
            {
                "name": "Молоко",
                "search_query": "молоко 3,2%",
                "quantity": "1,5",
                "unit": "л",
            },
            [self.candidate()],
        )

        self.assertEqual(match["quality"], "exact")
        self.assertEqual(match["package_count"], 2)

    def test_skips_an_exact_product_with_insufficient_stock(self):
        match = choose_product(
            {
                "name": "Молоко",
                "search_query": "молоко",
                "quantity": "1000",
                "unit": "мл",
            },
            [
                self.candidate(in_stock=1, weight="400 ml", name="Молоко 400 мл"),
                self.candidate(
                    product_id="product-87654321",
                    sku_id="product-87654321",
                    weight="1 l",
                    name="Молоко 1 л",
                    product_url=(
                        "https://eda.yandex.ru/retail/shop/product/product-87654321"
                        "?placeSlug=shop-nearby"
                    ),
                ),
            ],
        )

        self.assertEqual(match["product_id"], "product-87654321")
        self.assertEqual(match["package_count"], 1)

    def test_unexpected_material_modifier_is_a_reviewable_substitute(self):
        match = choose_product(
            {
                "name": "Молоко",
                "search_query": "молоко",
                "quantity": "500",
                "unit": "мл",
            },
            [self.candidate(name="Молоко овсяное 900 мл")],
        )

        self.assertEqual(match["quality"], "substitute")
        self.assertTrue(match["warning"])

    def test_required_dietary_modifier_cannot_be_dropped(self):
        for query, candidate_name in (
            ("молоко козье", "Молоко коровье 900 мл"),
            ("молоко без лактозы", "Молоко коровье 900 мл"),
            ("макароны безглютеновые", "Макароны пшеничные 450 г"),
        ):
            with self.subTest(query=query):
                match = choose_product(
                    {
                        "name": query.title(),
                        "search_query": query,
                        "quantity": "400",
                        "unit": "г",
                    },
                    [self.candidate(name=candidate_name, weight="400 г")],
                )

                self.assertEqual(match["quality"], "missing")
                self.assertEqual(match["package_count"], 0)

    def test_understands_yandex_english_multipack_weight(self):
        match = choose_product(
            {
                "name": "Яйца",
                "search_query": "яйца",
                "quantity": "12",
                "unit": "шт",
            },
            [
                self.candidate(
                    name="Яйца куриные 12 шт",
                    weight="2 x 6 pcs",
                )
            ],
        )

        self.assertEqual(match["package_count"], 1)

    def test_understands_full_russian_count_unit(self):
        match = choose_product(
            {
                "name": "Яйца",
                "search_query": "яйца",
                "quantity": "12",
                "unit": "шт",
            },
            [self.candidate(name="Яйца куриные 10 штук", weight="")],
        )

        self.assertEqual(match["quality"], "substitute")
        self.assertEqual(match["package_count"], 2)

    def test_uses_count_from_name_when_weight_has_incompatible_unit(self):
        match = choose_product(
            {
                "name": "Яйца",
                "search_query": "яйца",
                "quantity": "12",
                "unit": "шт",
            },
            [self.candidate(name="Яйца куриные 10 шт", weight="600 г")],
        )

        self.assertEqual(match["quality"], "substitute")
        self.assertEqual(match["package_count"], 2)

    def test_unknown_count_pack_size_never_multiplies_packages(self):
        match = choose_product(
            {
                "name": "Яйца",
                "search_query": "яйца",
                "quantity": "12",
                "unit": "шт",
            },
            [self.candidate(name="Яйца фермерские", weight="")],
        )

        self.assertEqual(match["quality"], "substitute")
        self.assertEqual(match["package_count"], 1)
        self.assertIn("Размер штучной упаковки", match["warning"])

    def test_non_finite_and_extreme_quantities_are_bounded(self):
        for quantity in ("NaN", "Infinity", "1e999999", "9" * 100):
            with self.subTest(quantity=quantity):
                match = choose_product(
                    {
                        "name": "Яйца",
                        "search_query": "яйца",
                        "quantity": quantity,
                        "unit": "шт",
                    },
                    [self.candidate(name="Яйца куриные 10 штук", weight="")],
                )

                self.assertEqual(match["quality"], "substitute")
                self.assertEqual(match["package_count"], 1)

    def test_single_word_query_with_extra_descriptor_requires_review(self):
        for query, product_name in (
            ("лук", "Лук-порей 1 шт"),
            ("перец", "Перец острый"),
        ):
            with self.subTest(query=query):
                match = choose_product(
                    {
                        "name": query.title(),
                        "search_query": query,
                        "quantity": "1",
                        "unit": "шт",
                    },
                    [self.candidate(name=product_name, weight="")],
                )

                self.assertEqual(match["quality"], "substitute")

    def test_shared_sku_must_have_stock_for_all_ingredient_deltas(self):
        ingredient = {
            "name": "Молоко",
            "search_query": "молоко 3,2%",
            "quantity": "1",
            "unit": "л",
        }
        matches = [
            choose_product(ingredient, [self.candidate(in_stock=3)]),
            choose_product(ingredient, [self.candidate(in_stock=3)]),
        ]

        enforce_aggregate_stock(matches)

        self.assertTrue(all(match["quality"] == "missing" for match in matches))
        self.assertTrue(all(match["package_count"] == 0 for match in matches))
        self.assertTrue(
            all("Суммарное количество" in match["warning"] for match in matches)
        )


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

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
    )
    @patch("recipes.carting.browser_login.httpx.Client")
    def test_scope_recovery_requires_controller_confirmation(self, client):
        response = client.return_value.__enter__.return_value.request.return_value
        response.status_code = 200
        response.json.return_value = {"status": "recovered"}

        recover_scope(cart_browser_session_key(self.user.pk))

        client.assert_called_once_with(timeout=120, trust_env=False)
        client.return_value.__enter__.return_value.request.assert_called_once_with(
            "POST",
            "http://browser.internal:9380/v1/recoveries",
            headers={"Authorization": "Bearer test-control-key"},
            json={"scope": cart_browser_session_key(self.user.pk)},
        )

    @override_settings(
        CART_BROWSER_CONTROL_URL="http://browser.internal:9380",
        CART_BROWSER_CONTROL_KEY="test-control-key",
    )
    @patch("recipes.carting.browser_login.httpx.Client")
    def test_scope_recovery_rejects_unconfirmed_response(self, client):
        response = client.return_value.__enter__.return_value.request.return_value
        response.status_code = 200
        response.json.return_value = {"status": "pending"}

        with self.assertRaisesMessage(
            BrowserLoginError,
            "Сервис браузера не подтвердил остановку операции.",
        ):
            recover_scope(cart_browser_session_key(self.user.pk))

    def test_agent_checks_store_delivery_before_searching_products(self):
        availability_check = ASSEMBLE_PROMPT.index("первым действием")
        product_search = ASSEMBLE_PROMPT.index("обрабатывай ингредиенты")

        self.assertLess(availability_check, product_search)
        self.assertIn("reason=store_unavailable", ASSEMBLE_PROMPT)

    def test_browser_session_key_is_stable_and_user_specific(self):
        self.assertEqual(cart_browser_session_key(self.user.pk), "recipes-cart-user-1")
        self.assertEqual(
            cart_browser_session_key(self.user.pk, 3),
            "recipes-cart-user-1-shard-3",
        )
        other_user = get_user_model().objects.create_user(username="other")
        self.assertNotEqual(
            cart_browser_session_key(self.user.pk, 3),
            cart_browser_session_key(other_user.pk, 3),
        )

    def test_manual_browser_login_blocks_worker_claim(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PENDING,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )
        BrowserLoginSession.objects.create(
            user=self.user,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        self.assertIsNone(claim_cart_run())
        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.PENDING)

    @patch("recipes.carting.pipeline.assemble_store_cart")
    def test_stop_before_next_store_prevents_another_browser_call(self, assemble):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PROCESSING,
            store_priority=["auchan", "lavka"],
            ingredient_snapshot=self.snapshot,
        )
        assemble.return_value = {
            "status": "failed",
            "items": [],
            "cart_cleared": True,
            "cart_mutated": False,
        }
        original_save = run.save

        def request_stop_after_store(*args, **kwargs):
            result = original_save(*args, **kwargs)
            if kwargs.get("update_fields") == ["next_store_index"]:
                CartRun.objects.filter(pk=run.pk).update(
                    cancellation_requested_at=timezone.now()
                )
            return result

        with patch.object(run, "save", side_effect=request_stop_after_store):
            process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(assemble.call_count, 1)
        self.assertEqual(run.status, CartRun.Status.CANCELLED)

    @patch("recipes.carting.pipeline.assemble_store_cart")
    def test_browser_operation_has_a_persisted_dispatch_reservation(self, assemble):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PROCESSING,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )

        def observe_reservation(*_args):
            run.refresh_from_db()
            self.assertIsNotNone(run.browser_operation_started_at)
            return {
                "status": "failed",
                "items": [],
                "cart_cleared": True,
                "cart_mutated": False,
            }

        assemble.side_effect = observe_reservation

        process_cart_run(run)

        run.refresh_from_db()
        self.assertIsNone(run.browser_operation_started_at)

    @patch("recipes.carting.pipeline.assemble_store_cart")
    def test_result_is_durable_before_browser_reservation_is_released(self, assemble):
        run = self.make_run()
        assemble.return_value = {
            "status": "exact",
            "cart_url": "https://eda.yandex.ru/cart",
            "summary": "Всё найдено",
            "cart_cleared": False,
            "items": [
                {
                    "ingredient_name": "Спагетти",
                    "requested_quantity": "400 г",
                    "product_name": "Спагетти 450 г",
                    "product_url": "https://eda.yandex.ru/product/sku-pasta-1",
                    "package_count": 1,
                    "added_package_count": 1,
                    "quality": "exact",
                    "warning": "",
                }
            ],
        }

        def request_stop_at_release(released_run):
            attempt = CartAttempt.objects.get(run=run)
            self.assertTrue(attempt.result["added_items"])
            run.refresh_from_db()
            self.assertIsNotNone(run.browser_operation_started_at)
            CartRun.objects.filter(pk=run.pk).update(
                cancellation_requested_at=timezone.now()
            )
            _release_browser_operation(released_run)

        with patch(
            "recipes.carting.pipeline._release_browser_operation",
            side_effect=request_stop_at_release,
        ):
            process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.CANCELLED)
        self.assertIsNone(run.cleaned_at)
        self.assertIn("могли остаться товары", run.error)

    @patch(
        "recipes.management.commands.run_cart_worker.expire_unconfirmed_cart_runs",
        return_value=0,
    )
    @patch(
        "recipes.management.commands.run_cart_worker.claim_cleanup_run",
        return_value=None,
    )
    @patch("recipes.management.commands.run_cart_worker.claim_cart_run")
    @patch("recipes.management.commands.run_cart_worker.process_cart_run")
    def test_worker_rechecks_stop_after_saving_an_error(
        self,
        process_run,
        claim_run,
        _claim_cleanup,
        _expire,
    ):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PROCESSING,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )
        claim_run.return_value = run
        process_run.side_effect = CartAgentError("safe failure")
        real_finish = finish_requested_cart_stop
        finish_calls = 0

        def stop_between_check_and_error_save(*args, **kwargs):
            nonlocal finish_calls
            finish_calls += 1
            result = real_finish(*args, **kwargs)
            if finish_calls == 1:
                CartRun.objects.filter(pk=run.pk).update(
                    cancellation_requested_at=timezone.now()
                )
            return result

        with patch(
            "recipes.management.commands.run_cart_worker.finish_requested_cart_stop",
            side_effect=stop_between_check_and_error_save,
        ):
            call_command("run_cart_worker", "--once")

        run.refresh_from_db()
        self.assertGreaterEqual(finish_calls, 2)
        self.assertEqual(run.status, CartRun.Status.CANCELLED)

    @patch("recipes.carting.coordination.stop_session")
    def test_expired_login_still_blocks_when_remote_close_is_uncertain(self, stop_session):
        stop_session.side_effect = BrowserLoginError("controller unavailable")
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PENDING,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )
        login_session = BrowserLoginSession.objects.create(
            user=self.user,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.ACTIVE,
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        self.assertIsNone(claim_cart_run())

        run.refresh_from_db()
        login_session.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.PENDING)
        self.assertEqual(login_session.status, BrowserLoginSession.Status.ACTIVE)

    @patch("recipes.carting.coordination.stop_session")
    def test_expired_login_releases_worker_after_confirmed_close(self, stop_session):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PENDING,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )
        login_session = BrowserLoginSession.objects.create(
            user=self.user,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.ACTIVE,
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        claimed = claim_cart_run()

        login_session.refresh_from_db()
        self.assertEqual(claimed, run)
        self.assertEqual(login_session.status, BrowserLoginSession.Status.EXPIRED)
        stop_session.assert_called_once_with(login_session.remote_session_id)

    @patch("recipes.carting.coordination.stop_session")
    def test_interrupted_login_stop_is_reconciled_after_transition_grace(
        self,
        stop_session,
    ):
        stopping_run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            cancellation_requested_at=timezone.now() - timedelta(seconds=31),
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )
        pending_run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PENDING,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )
        login_session = BrowserLoginSession.objects.create(
            user=self.user,
            run=stopping_run,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.STOPPING,
            transition_started_at=timezone.now() - timedelta(seconds=31),
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        claimed = claim_cart_run()

        login_session.refresh_from_db()
        stopping_run.refresh_from_db()
        self.assertEqual(claimed, pending_run)
        self.assertEqual(login_session.status, BrowserLoginSession.Status.FAILED)
        self.assertEqual(stopping_run.status, CartRun.Status.CANCELLED)
        self.assertIsNone(stopping_run.cleaned_at)
        self.assertIn("могли остаться товары", stopping_run.error)
        stop_session.assert_called_once_with(login_session.remote_session_id)

    @patch("recipes.carting.coordination.stop_session")
    def test_interrupted_login_completion_is_reconciled_and_resumes_run(
        self,
        stop_session,
    ):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.LOGIN_REQUIRED,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )
        login_session = BrowserLoginSession.objects.create(
            user=self.user,
            run=run,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.COMPLETING,
            transition_started_at=timezone.now() - timedelta(seconds=31),
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        claimed = claim_cart_run()

        login_session.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(claimed, run)
        self.assertEqual(login_session.status, BrowserLoginSession.Status.COMPLETED)
        self.assertEqual(run.status, CartRun.Status.PROCESSING)
        stop_session.assert_called_once_with(login_session.remote_session_id)

    @patch("recipes.carting.coordination.stop_session")
    def test_fresh_login_transition_keeps_worker_blocked_without_duplicate_close(
        self,
        stop_session,
    ):
        pending_run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PENDING,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )
        BrowserLoginSession.objects.create(
            user=self.user,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.STOPPING,
            transition_started_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        self.assertIsNone(claim_cart_run())

        pending_run.refresh_from_db()
        self.assertEqual(pending_run.status, CartRun.Status.PENDING)
        stop_session.assert_not_called()

    def test_recent_browser_operation_is_not_recovered_from_old_run_start(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PROCESSING,
            started_at=timezone.now() - timedelta(minutes=31),
            browser_operation_started_at=timezone.now(),
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )

        self.assertEqual(CartWorkerCommand.recover_stale_jobs(), 0)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.PROCESSING)
        self.assertIsNotNone(run.browser_operation_started_at)

    @patch("recipes.carting.pipeline.assemble_store_cart")
    def test_old_worker_does_not_persist_after_its_operation_is_recovered(
        self,
        assemble,
    ):
        run = self.make_run()

        def recover_during_browser_call(*_args):
            CartRun.objects.filter(pk=run.pk).update(
                browser_operation_started_at=timezone.now() - timedelta(minutes=31)
            )
            self.assertEqual(CartWorkerCommand.recover_stale_jobs(), 1)
            return {
                "status": "exact",
                "cart_url": "https://eda.yandex.ru/cart",
                "items": [],
            }

        assemble.side_effect = recover_during_browser_call

        process_cart_run(run)

        run.refresh_from_db()
        attempt = run.attempts.get()
        self.assertEqual(run.status, CartRun.Status.MANUAL_CHECK)
        self.assertEqual(run.next_store_index, 0)
        self.assertEqual(attempt.status, CartAttempt.Status.PROCESSING)
        self.assertEqual(attempt.result, {})

    @patch("recipes.carting.coordination.stop_session")
    def test_transition_without_timestamp_is_reconciled(self, stop_session):
        pending_run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PENDING,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )
        login_session = BrowserLoginSession.objects.create(
            user=self.user,
            remote_session_id="remote-session-id-1234567890",
            status=BrowserLoginSession.Status.STOPPING,
            transition_started_at=None,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        claimed = claim_cart_run()

        login_session.refresh_from_db()
        self.assertEqual(claimed, pending_run)
        self.assertEqual(login_session.status, BrowserLoginSession.Status.FAILED)
        stop_session.assert_called_once_with(login_session.remote_session_id)

    def test_stale_worker_with_browser_reservation_requires_manual_check(self):
        uncertain_run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PROCESSING,
            started_at=timezone.now() - timedelta(minutes=31),
            browser_operation_started_at=timezone.now() - timedelta(minutes=31),
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )
        pending_run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PENDING,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )

        self.assertEqual(CartWorkerCommand.recover_stale_jobs(), 1)

        uncertain_run.refresh_from_db()
        self.assertEqual(uncertain_run.status, CartRun.Status.MANUAL_CHECK)
        self.assertIsNotNone(uncertain_run.browser_operation_started_at)
        self.assertIn("Проверьте корзину вручную", uncertain_run.error)
        self.assertIsNone(claim_cart_run())
        pending_run.refresh_from_db()
        self.assertEqual(pending_run.status, CartRun.Status.PENDING)

        other_user = get_user_model().objects.create_user(username="other-cook")
        other_recipe = Recipe.objects.create(
            title="Суп",
            servings=2,
            created_by=other_user,
        )
        other_run = CartRun.objects.create(
            recipe=other_recipe,
            requested_by=other_user,
            servings=2,
            status=CartRun.Status.PENDING,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )

        self.assertIsNone(claim_cart_run())

    def test_regular_manual_check_only_blocks_its_user(self):
        CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.MANUAL_CHECK,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )
        blocked_run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PENDING,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )
        other_user = get_user_model().objects.create_user(username="manual-other-cook")
        other_recipe = Recipe.objects.create(
            title="Салат",
            servings=2,
            created_by=other_user,
        )
        other_run = CartRun.objects.create(
            recipe=other_recipe,
            requested_by=other_user,
            servings=2,
            status=CartRun.Status.PENDING,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )

        self.assertEqual(claim_cart_run(), other_run)
        blocked_run.refresh_from_db()
        self.assertEqual(blocked_run.status, CartRun.Status.PENDING)

    def test_stale_worker_without_browser_reservation_can_be_requeued(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.PROCESSING,
            started_at=timezone.now() - timedelta(minutes=31),
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )

        self.assertEqual(CartWorkerCommand.recover_stale_jobs(), 1)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.PENDING)
        self.assertIsNone(run.browser_operation_started_at)

    def test_failed_run_with_orphaned_reservation_is_recoverable(self):
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.FAILED,
            browser_operation_started_at=timezone.now() - timedelta(minutes=31),
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
        )

        self.assertEqual(CartWorkerCommand.recover_stale_jobs(), 1)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.MANUAL_CHECK)
        self.assertIsNotNone(run.browser_operation_started_at)
        self.assertIn("Проверьте корзину вручную", run.error)

    @patch("recipes.carting.pipeline.assemble_store_cart")
    def test_uncertain_remote_assembly_keeps_global_reservation(
        self,
        assemble,
    ):
        assemble.side_effect = CartAgentError(
            "controller timeout",
            mutation_possible=True,
            operation_uncertain=True,
        )
        run = self.make_run()

        process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.MANUAL_CHECK)
        self.assertIsNotNone(run.browser_operation_started_at)
        self.assertTrue(run.selected_attempt.result["operation_uncertain"])

    @patch("recipes.carting.pipeline.assemble_store_cart")
    def test_unexpected_assembly_error_releases_reservation_to_manual_check(
        self,
        assemble,
    ):
        assemble.side_effect = RuntimeError("malformed browser response")
        run = self.make_run()

        with self.assertRaises(CartAgentError) as caught:
            process_cart_run(run)

        run.refresh_from_db()
        attempt = run.attempts.get()
        self.assertTrue(caught.exception.mutation_possible)
        self.assertEqual(run.status, CartRun.Status.MANUAL_CHECK)
        self.assertIsNone(run.browser_operation_started_at)
        self.assertEqual(run.selected_attempt, attempt)
        self.assertTrue(attempt.result["mutation_unknown"])

    @patch("recipes.carting.pipeline.cleanup_store_cart")
    def test_unexpected_cleanup_error_releases_reservation_to_manual_check(
        self,
        cleanup,
    ):
        cleanup.side_effect = RuntimeError("malformed cleanup response")
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.CLEANING,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
            cleanup_requested_at=timezone.now(),
        )
        attempt = CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.EXACT,
            cart_url="https://eda.yandex.ru/cart",
            result={
                "added_items": [
                    {
                        "product_name": "Спагетти 450 г",
                        "product_url": "https://eda.yandex.ru/product/sku-pasta-12345678",
                        "product_id": "sku-pasta-12345678",
                        "package_count": 1,
                        "added_package_count": 1,
                    }
                ]
            },
        )
        run.selected_attempt = attempt
        run.save(update_fields=["selected_attempt"])

        with self.assertRaises(CartAgentError) as caught:
            process_cart_cleanup(run)

        run.refresh_from_db()
        attempt.refresh_from_db()
        self.assertTrue(caught.exception.mutation_possible)
        self.assertEqual(run.status, CartRun.Status.MANUAL_CHECK)
        self.assertIsNone(run.browser_operation_started_at)
        self.assertTrue(attempt.result["mutation_unknown"])

    @patch("recipes.carting.pipeline.cleanup_store_cart")
    def test_uncertain_remote_cleanup_keeps_global_reservation(self, cleanup):
        cleanup.side_effect = CartAgentError(
            "controller timeout",
            mutation_possible=True,
            operation_uncertain=True,
        )
        run = CartRun.objects.create(
            recipe=self.recipe,
            requested_by=self.user,
            servings=2,
            status=CartRun.Status.CLEANING,
            store_priority=["auchan"],
            ingredient_snapshot=self.snapshot,
            cleanup_requested_at=timezone.now(),
        )
        attempt = CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.EXACT,
            cart_url="https://eda.yandex.ru/cart",
            result={
                "added_items": [
                    {
                        "product_name": "Спагетти 450 г",
                        "product_url": "https://eda.yandex.ru/product/sku-pasta-12345678",
                        "product_id": "sku-pasta-12345678",
                        "package_count": 1,
                        "added_package_count": 1,
                    }
                ]
            },
        )
        run.selected_attempt = attempt
        run.save(update_fields=["selected_attempt"])

        process_cart_cleanup(run)

        run.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.MANUAL_CHECK)
        self.assertIsNotNone(run.browser_operation_started_at)
        self.assertTrue(attempt.result["operation_uncertain"])

    @patch("recipes.carting.client.run_store_cart_task")
    def test_single_ingredient_keeps_legacy_agent_call(self, run_task):
        run = self.make_run()
        run_task.return_value = {"status": "exact", "items": []}

        self.assertEqual(assemble_store_cart(run, "auchan"), run_task.return_value)

        run_task.assert_called_once_with(run, "auchan", "assemble")

    @patch("recipes.carting.client.run_store_cart_task")
    def test_cleanup_keeps_single_unsharded_agent_call(self, run_task):
        run = self.make_run()
        added_items = [{"product_id": "sku-pasta-1", "package_count": 1}]
        run_task.return_value = {"status": "cleared"}

        result = cleanup_store_cart(
            run,
            "auchan",
            added_items,
            "https://eda.yandex.ru/cart",
        )

        self.assertEqual(result, {"status": "cleared"})
        run_task.assert_called_once_with(
            run,
            "auchan",
            "cleanup",
            added_items=added_items,
            cart_url="https://eda.yandex.ru/cart",
        )

    @override_settings(
        CART_ADAPTER_BASE_URL="https://adapter.example",
        CART_ADAPTER_API_KEY="adapter-key",
        CART_ADAPTER_FALLBACK_TO_HERMES=True,
    )
    @patch("recipes.carting.client.run_store_cart_task")
    @patch("recipes.carting.client._run_adapter_task")
    def test_fast_adapter_searches_then_applies_without_hermes(
        self, adapter_task, run_task
    ):
        product_id = "12345678-1234-1234-1234-123456789abc"
        product_url = (
            f"https://eda.yandex.ru/retail/auchan/product/{product_id}"
            "?placeSlug=auchan-nearby"
        )
        adapter_task.side_effect = [
            {
                "status": "ready",
                "selection_token": "signed-selection",
                "cart_url": "https://eda.yandex.ru/retail/auchan?placeSlug=auchan-nearby",
                "elapsed_ms": 450,
                "results": [
                    {
                        "index": 0,
                        "candidates": [
                            {
                                "product_id": product_id,
                                "sku_id": product_id,
                                "name": "Спагетти 450 г",
                                "weight": "450 g",
                                "available": True,
                                "in_stock": 8,
                                "product_url": product_url,
                            }
                        ],
                    }
                ],
            },
            {
                "status": "applied",
                "cart_url": "https://eda.yandex.ru/retail/auchan?placeSlug=auchan-nearby",
                "elapsed_ms": 300,
                "additions": [{"product_id": product_id, "added_quantity": 1}],
                "cleanup_token": "signed-cleanup",
            },
        ]
        run = self.make_run()

        result = assemble_store_cart(run, "auchan")

        self.assertEqual(result["status"], "exact")
        self.assertEqual(result["provider"], "yandex_api_adapter")
        self.assertEqual(result["items"][0]["package_count"], 1)
        self.assertEqual(result["items"][0]["added_package_count"], 1)
        self.assertEqual(result["cleanup_token"], "signed-cleanup")
        self.assertEqual(result["timings_ms"], {"search": 450, "apply": 300})
        self.assertEqual(adapter_task.call_count, 2)
        self.assertEqual(adapter_task.call_args_list[0].args[0], "/v1/search")
        self.assertEqual(adapter_task.call_args_list[1].args[0], "/v1/apply")
        search_operation = adapter_task.call_args_list[0].args[1]["operation_id"]
        apply_operation = adapter_task.call_args_list[1].args[1]["operation_id"]
        self.assertEqual(search_operation, apply_operation)
        self.assertRegex(
            search_operation,
            rf"^cart-run-{run.pk}-[0-9]{{20}}-auchan$",
        )
        self.assertFalse(adapter_task.call_args_list[0].kwargs["mutation_possible"])
        self.assertTrue(adapter_task.call_args_list[1].kwargs["mutation_possible"])
        run_task.assert_not_called()

    @override_settings(
        CART_ADAPTER_BASE_URL="https://adapter.example",
        CART_ADAPTER_API_KEY="adapter-key",
        CART_ADAPTER_FALLBACK_TO_HERMES=True,
    )
    @patch("recipes.carting.client.run_store_cart_task")
    @patch("recipes.carting.client._search_with_adapter")
    def test_adapter_failure_before_search_mutation_falls_back_to_hermes(
        self, adapter_search, run_task
    ):
        adapter_search.side_effect = CartAgentError("Адаптер недоступен")
        run_task.return_value = {"status": "exact", "items": []}
        run = self.make_run()

        result = assemble_store_cart(run, "auchan")

        self.assertEqual(result, run_task.return_value)
        run_task.assert_called_once_with(run, "auchan", "assemble")

    @override_settings(
        CART_ADAPTER_BASE_URL="https://adapter.example",
        CART_ADAPTER_API_KEY="adapter-key",
        CART_ADAPTER_FALLBACK_TO_HERMES=True,
    )
    @patch("recipes.carting.client.run_store_cart_task")
    @patch("recipes.carting.client._run_adapter_task")
    def test_uncertain_search_status_never_releases_profile_to_hermes(
        self, adapter_task, run_task
    ):
        for status in ("login_required", "blocked", "incomplete"):
            with self.subTest(status=status):
                adapter_task.return_value = {
                    "status": status,
                    "summary": "Профиль мог остаться открытым.",
                    "mutation_possible": True,
                }

                with self.assertRaises(CartAgentError) as caught:
                    assemble_store_cart(self.make_run(), "auchan")

                self.assertTrue(caught.exception.mutation_possible)
        run_task.assert_not_called()

    @override_settings(
        CART_ADAPTER_BASE_URL="https://adapter.example",
        CART_ADAPTER_API_KEY="adapter-key",
    )
    @patch("recipes.carting.client.httpx.Client")
    def test_adapter_transport_timeout_never_allows_concurrent_fallback(self, client):
        client.return_value.__enter__.return_value.post.side_effect = (
            httpx.ReadTimeout("adapter timed out")
        )

        with self.assertRaises(CartAgentError) as caught:
            _run_adapter_task(
                "/v1/search",
                {"scope": "recipes-cart-user-1", "store": "auchan"},
                mutation_possible=False,
            )

        self.assertTrue(caught.exception.mutation_possible)
        self.assertTrue(caught.exception.operation_uncertain)
        client.assert_called_once_with(
            timeout=settings.CART_ADAPTER_TIMEOUT_SECONDS,
            trust_env=False,
            verify=True,
        )

    @override_settings(
        CART_AI_BASE_URL="https://agent.example",
        CART_AI_MODEL="browser-agent",
    )
    @patch("recipes.carting.client.httpx.Client")
    def test_agent_transport_timeout_marks_operation_uncertain(self, client):
        client.return_value.__enter__.return_value.post.side_effect = (
            httpx.ReadTimeout("agent timed out")
        )

        with self.assertRaises(CartAgentError) as caught:
            run_store_cart_task(self.make_run(), "auchan", "assemble")

        self.assertTrue(caught.exception.mutation_possible)
        self.assertTrue(caught.exception.operation_uncertain)

    @override_settings(
        CART_ADAPTER_BASE_URL="http://adapter.example",
        CART_ADAPTER_API_KEY="adapter-key",
    )
    @patch("recipes.carting.client.httpx.Client")
    def test_adapter_rejects_plain_http_outside_loopback(self, client):
        with self.assertRaisesMessage(
            CartAgentError,
            "Небезопасное подключение к адаптеру корзины запрещено.",
        ):
            _run_adapter_task(
                "/v1/search",
                {"scope": "recipes-cart-user-1", "store": "auchan"},
                mutation_possible=False,
            )

        client.assert_not_called()

    @override_settings(
        CART_ADAPTER_BASE_URL="https://adapter.example",
        CART_ADAPTER_API_KEY="adapter-key",
    )
    @patch("recipes.carting.client.httpx.Client")
    def test_adapter_trusts_explicit_safe_http_rejection(self, client):
        response = client.return_value.__enter__.return_value.post.return_value
        response.json.return_value = {
            "status": "failed",
            "summary": "Запрос отклонён до изменения корзины.",
            "mutation_possible": False,
        }
        response.is_error = True

        with self.assertRaises(CartAgentError) as caught:
            _run_adapter_task(
                "/v1/apply",
                {"scope": "recipes-cart-user-1", "store": "auchan"},
                mutation_possible=True,
            )

        self.assertFalse(caught.exception.mutation_possible)

    @override_settings(
        CART_ADAPTER_BASE_URL="https://adapter.example",
        CART_ADAPTER_API_KEY="adapter-key",
        CART_ADAPTER_FALLBACK_TO_HERMES=True,
    )
    @patch("recipes.carting.client.run_store_cart_task")
    @patch("recipes.carting.client._run_adapter_task")
    def test_adapter_never_falls_back_after_apply_was_dispatched(
        self, adapter_task, run_task
    ):
        product_id = "12345678-1234-1234-1234-123456789abc"
        adapter_task.side_effect = [
            {
                "status": "ready",
                "selection_token": "signed-selection",
                "cart_url": "https://eda.yandex.ru/retail/auchan?placeSlug=auchan-nearby",
                "results": [
                    {
                        "index": 0,
                        "candidates": [
                            {
                                "product_id": product_id,
                                "sku_id": product_id,
                                "name": "Спагетти 450 г",
                                "weight": "450 g",
                                "available": True,
                                "in_stock": 8,
                                "product_url": (
                                    "https://eda.yandex.ru/retail/auchan/product/"
                                    f"{product_id}?placeSlug=auchan-nearby"
                                ),
                            }
                        ],
                    }
                ],
            },
            {
                "status": "failed",
                "summary": "Несовместимый ответ без признака мутации",
            },
        ]
        run = self.make_run()

        with self.assertRaises(CartAgentError) as raised:
            assemble_store_cart(run, "auchan")

        self.assertTrue(raised.exception.mutation_possible)
        run_task.assert_not_called()

    @override_settings(
        CART_ADAPTER_BASE_URL="https://adapter.example",
        CART_ADAPTER_API_KEY="adapter-key",
        CART_ADAPTER_FALLBACK_TO_HERMES=False,
    )
    @patch("recipes.carting.client._run_adapter_task")
    def test_adapter_safe_apply_rejection_does_not_require_manual_check(
        self, adapter_task
    ):
        product_id = "12345678-1234-1234-1234-123456789abc"
        adapter_task.side_effect = [
            {
                "status": "ready",
                "selection_token": "signed-selection",
                "cart_url": "https://eda.yandex.ru/retail/auchan?placeSlug=auchan-nearby",
                "results": [
                    {
                        "index": 0,
                        "candidates": [
                            {
                                "product_id": product_id,
                                "sku_id": product_id,
                                "name": "Спагетти 450 г",
                                "weight": "450 g",
                                "available": True,
                                "in_stock": 8,
                                "product_url": (
                                    "https://eda.yandex.ru/retail/auchan/product/"
                                    f"{product_id}?placeSlug=auchan-nearby"
                                ),
                            }
                        ],
                    }
                ],
            },
            {
                "status": "failed",
                "summary": "Запрос отклонён до изменения корзины.",
                "mutation_possible": False,
            },
        ]
        run = self.make_run()

        with self.assertRaises(CartAgentError) as raised:
            assemble_store_cart(run, "auchan")

        self.assertFalse(raised.exception.mutation_possible)

    @override_settings(
        CART_ADAPTER_BASE_URL="https://adapter.example",
        CART_ADAPTER_API_KEY="adapter-key",
        CART_ADAPTER_FALLBACK_TO_HERMES=True,
    )
    @patch("recipes.carting.client.run_store_cart_task")
    @patch("recipes.carting.client._run_adapter_task")
    def test_incomplete_successful_apply_requires_manual_check(
        self, adapter_task, run_task
    ):
        product_id = "12345678-1234-1234-1234-123456789abc"
        adapter_task.side_effect = [
            {
                "status": "ready",
                "selection_token": "signed-selection",
                "cart_url": "https://eda.yandex.ru/retail/auchan?placeSlug=auchan-nearby",
                "results": [
                    {
                        "index": 0,
                        "candidates": [
                            {
                                "product_id": product_id,
                                "sku_id": product_id,
                                "name": "Спагетти 450 г",
                                "weight": "450 g",
                                "available": True,
                                "in_stock": 8,
                                "product_url": (
                                    "https://eda.yandex.ru/retail/auchan/product/"
                                    f"{product_id}?placeSlug=auchan-nearby"
                                ),
                            }
                        ],
                    }
                ],
            },
            {"status": "applied", "cart_url": "https://eda.yandex.ru/cart"},
        ]
        run = self.make_run()

        with self.assertRaises(CartAgentError) as raised:
            assemble_store_cart(run, "auchan")

        self.assertTrue(raised.exception.mutation_possible)
        run_task.assert_not_called()

    @override_settings(
        CART_ADAPTER_BASE_URL="https://adapter.example",
        CART_ADAPTER_API_KEY="adapter-key",
    )
    @patch("recipes.carting.client._run_adapter_task")
    def test_adapter_does_not_mutate_when_nothing_matches(self, adapter_task):
        adapter_task.return_value = {
            "status": "ready",
            "selection_token": "signed-selection",
            "cart_url": "https://eda.yandex.ru/retail/auchan?placeSlug=auchan-nearby",
            "results": [{"index": 0, "candidates": []}],
        }
        run = self.make_run()

        result = assemble_store_cart(run, "auchan")

        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(result["cart_cleared"])
        self.assertEqual(result["items"][0]["quality"], "missing")
        adapter_task.assert_called_once()

    @override_settings(
        CART_ADAPTER_BASE_URL="https://adapter.example",
        CART_ADAPTER_API_KEY="adapter-key",
    )
    @patch("recipes.carting.client._run_adapter_task")
    def test_cleanup_uses_exact_adapter_journal(self, adapter_task):
        adapter_task.return_value = {"status": "cleared", "summary": "Очищено"}
        run = self.make_run()

        result = cleanup_store_cart(
            run,
            "auchan",
            [{"product_id": "product-12345678", "package_count": 2}],
            "https://eda.yandex.ru/cart",
            cleanup_token="signed-cleanup",
        )

        self.assertEqual(result["status"], "cleared")
        adapter_task.assert_called_once_with(
            "/v1/cleanup",
            {
                "scope": "recipes-cart-user-1",
                "store": "auchan",
                "cleanup_token": "signed-cleanup",
            },
            mutation_possible=True,
        )

    @override_settings(
        CART_ADAPTER_BASE_URL="https://adapter.example",
        CART_ADAPTER_API_KEY="adapter-key",
    )
    @patch("recipes.carting.client._run_adapter_task")
    def test_uncertain_adapter_cleanup_block_requires_manual_check(self, adapter_task):
        adapter_task.return_value = {
            "status": "blocked",
            "summary": "Нужна ручная проверка",
            "mutation_possible": True,
        }
        run = self.make_run()

        with self.assertRaises(CartAgentError) as caught:
            cleanup_store_cart(
                run,
                "auchan",
                [{"product_id": "product-12345678", "package_count": 2}],
                "https://eda.yandex.ru/cart",
                cleanup_token="signed-cleanup",
            )

        self.assertTrue(caught.exception.mutation_possible)

    @override_settings(
        CART_ADAPTER_BASE_URL="https://adapter.example",
        CART_ADAPTER_API_KEY="adapter-key",
    )
    @patch("recipes.carting.client._run_adapter_task")
    def test_safe_adapter_cleanup_requests_manual_removal(self, adapter_task):
        adapter_task.return_value = {
            "status": "login_required",
            "summary": "Удалите добавления вручную",
            "mutation_possible": False,
        }
        run = self.make_run()

        result = cleanup_store_cart(
            run,
            "auchan",
            [{"product_id": "product-12345678", "package_count": 2}],
            "https://eda.yandex.ru/cart",
            cleanup_token="signed-cleanup",
        )

        self.assertEqual(result["status"], "login_required")
        self.assertFalse(result["mutation_possible"])

    @override_settings(
        CART_ADAPTER_BASE_URL="",
        CART_ADAPTER_API_KEY="",
    )
    @patch("recipes.carting.client.run_store_cart_task")
    def test_signed_cleanup_never_falls_back_to_hermes(self, run_task):
        run = self.make_run()

        with self.assertRaises(CartAgentError) as caught:
            cleanup_store_cart(
                run,
                "auchan",
                [{"product_id": "product-12345678", "package_count": 2}],
                "https://eda.yandex.ru/cart",
                cleanup_token="signed-cleanup",
            )

        self.assertTrue(caught.exception.mutation_possible)
        run_task.assert_not_called()

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
                    "product_url": "https://eda.yandex.ru/product/sku-pasta-1",
                    "package_count": 1,
                }
            ],
            "items": [
                {
                    "ingredient_name": "Спагетти",
                    "requested_quantity": "400 г",
                    "product_name": "Спагетти 450 г",
                    "product_url": "https://eda.yandex.ru/product/sku-pasta-1",
                    "package_count": 1,
                    "added_package_count": 1,
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
    def test_cleanup_journal_uses_only_bounded_snapshot_items(self, assemble):
        assemble.return_value = {
            "status": "exact",
            "cart_url": "https://eda.yandex.ru/cart",
            "cart_cleared": False,
            "added_items": [
                {
                    "product_name": "Чужой товар из прежней корзины",
                    "product_url": "https://eda.yandex.ru/product/pre-existing",
                    "package_count": 99,
                }
            ],
            "items": [
                {
                    "ingredient_name": "Не из snapshot",
                    "product_name": "Ещё один чужой товар",
                    "product_url": "https://eda.yandex.ru/product/injected",
                    "package_count": 50,
                    "added_package_count": 50,
                    "quality": "exact",
                },
                {
                    "ingredient_name": "Спагетти",
                    "requested_quantity": "400 г",
                    "product_name": "Спагетти 450 г",
                    "product_url": "https://eda.yandex.ru/product/legitimate",
                    "package_count": 2,
                    "added_package_count": 99,
                    "quality": "exact",
                    "warning": "",
                },
            ],
        }
        run = self.make_run()

        process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(
            run.selected_attempt.result["added_items"],
            [
                {
                    "product_name": "Спагетти 450 г",
                    "product_url": "https://eda.yandex.ru/product/legitimate",
                    "product_id": "legitimate",
                    "package_count": 2,
                    "added_package_count": 2,
                }
            ],
        )

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
                    "product_url": "https://eda.yandex.ru/product/sku-noodles-2",
                    "package_count": 1,
                    "added_package_count": 1,
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
    def test_unavailable_selected_store_fails_before_trying_another_store(self, assemble):
        assemble.return_value = {
            "status": "failed",
            "reason": "store_unavailable",
            "summary": "Ашан недоступен по сохранённому адресу.",
            "cart_cleared": True,
            "items": [],
        }
        run = self.make_run(["auchan", "perekrestok"])

        process_cart_run(run)

        run.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.FAILED)
        self.assertEqual(run.error, assemble.return_value["summary"])
        self.assertEqual(run.selected_attempt.store, "auchan")
        self.assertEqual(assemble.call_count, 1)

    @patch("recipes.carting.pipeline.cleanup_store_cart")
    @patch("recipes.carting.pipeline.assemble_store_cart")
    def test_unavailable_store_with_additions_is_cleaned_before_failure(
        self,
        assemble,
        cleanup,
    ):
        assemble.return_value = {
            "status": "failed",
            "reason": "store_unavailable",
            "summary": "Противоречивый ответ",
            "cart_url": "https://eda.yandex.ru/cart",
            "cart_cleared": False,
            "items": [
                {
                    "ingredient_name": "Спагетти",
                    "product_name": "Спагетти 450 г",
                    "product_url": "https://eda.yandex.ru/product/sku-pasta-1",
                    "package_count": 1,
                    "added_package_count": 1,
                    "quality": "exact",
                }
            ],
        }
        cleanup.return_value = {"status": "cleared", "summary": "Очищено"}
        run = self.make_run(["auchan", "perekrestok"])

        process_cart_run(run)

        run.refresh_from_db()
        attempt = run.attempts.get(store="auchan")
        attempt.refresh_from_db()
        self.assertEqual(run.status, CartRun.Status.FAILED)
        self.assertEqual(attempt.status, CartAttempt.Status.FAILED)
        self.assertTrue(attempt.result["cart_cleared"])
        self.assertFalse(attempt.result["mutation_unknown"])
        self.assertEqual(assemble.call_count, 1)
        cleanup.assert_called_once()

    @patch("recipes.carting.pipeline.assemble_store_cart")
    def test_unavailable_store_without_cleanup_proof_requires_manual_check(
        self,
        assemble,
    ):
        assemble.return_value = {
            "status": "failed",
            "reason": "store_unavailable",
            "summary": "Не удалось проверить доступность",
            "cart_cleared": False,
            "items": [],
        }
        run = self.make_run(["auchan", "perekrestok"])

        process_cart_run(run)

        run.refresh_from_db()
        attempt = run.attempts.get(store="auchan")
        self.assertEqual(run.status, CartRun.Status.MANUAL_CHECK)
        self.assertTrue(attempt.result["mutation_unknown"])
        self.assertEqual(
            attempt.result["validation_error"],
            "inconsistent_store_unavailable_result",
        )
        self.assertEqual(assemble.call_count, 1)

    @patch("recipes.carting.pipeline.assemble_store_cart")
    def test_unknown_mutation_requires_manual_check_before_retry(self, assemble):
        assemble.side_effect = CartAgentError(
            "Соединение оборвалось",
            mutation_possible=True,
        )
        run = self.make_run(["auchan", "perekrestok"])

        process_cart_run(run)

        run.refresh_from_db()
        attempt = run.attempts.get()
        self.assertEqual(run.status, CartRun.Status.MANUAL_CHECK)
        self.assertEqual(run.next_store_index, 0)
        self.assertEqual(run.selected_attempt, attempt)
        self.assertTrue(attempt.result["mutation_unknown"])
        self.assertEqual(attempt.result["error"], "Соединение оборвалось")

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
                    "added_package_count": 1 if index == 0 else 0,
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
                    "product_url": f"https://eda.yandex.ru/product/sku-{index:08d}",
                    "package_count": 1,
                    "added_package_count": 1,
                    "quality": "exact",
                    "warning": "",
                }
                for index, name in enumerate(
                    ["Первый", "Второй", "Третий", "Четвёртый"],
                    start=1,
                )
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
                    "added_package_count": 1 if index < 2 else 0,
                    "product_name": f"{name} товар" if index < 2 else "",
                    "product_url": (
                        f"https://eda.yandex.ru/product/sku-{index:08d}"
                        if index < 2
                        else ""
                    ),
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
    def test_expired_cart_uses_signed_cleanup_and_actual_added_quantity(self, cleanup):
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
                "cleanup_token": "signed-cleanup",
                "added_items": [
                    {
                        "product_name": "Спагетти",
                        "product_url": "https://eda.yandex.ru/product/sku-pasta-1",
                        "package_count": 50,
                        "added_package_count": 2,
                    }
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
            [
                {
                    "product_name": "Спагетти",
                    "product_url": "https://eda.yandex.ru/product/sku-pasta-1",
                    "product_id": "sku-pasta-1",
                    "package_count": 2,
                    "added_package_count": 2,
                }
            ],
            "https://eda.yandex.ru/cart",
            cleanup_token="signed-cleanup",
        )

    @patch("recipes.carting.pipeline.cleanup_store_cart")
    def test_cleanup_stops_when_product_has_no_stable_id(self, cleanup):
        run = self.make_run()
        run.status = CartRun.Status.CLEANING
        run.save(update_fields=["status"])
        attempt = CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.EXACT,
            result={
                "cart_cleared": False,
                "added_items": [
                    {
                        "product_name": "Спагетти",
                        "product_url": "",
                        "package_count": 2,
                    }
                ],
            },
        )
        run.selected_attempt = attempt
        run.save(update_fields=["selected_attempt"])

        with self.assertRaisesMessage(CartAgentError, "нельзя однозначно"):
            process_cart_cleanup(run)

        cleanup.assert_not_called()

    @patch("recipes.carting.pipeline.cleanup_store_cart")
    def test_adapter_attempt_without_signed_cleanup_never_uses_hermes(self, cleanup):
        run = self.make_run()
        run.status = CartRun.Status.CLEANING
        run.save(update_fields=["status"])
        attempt = CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.EXACT,
            cart_url="https://eda.yandex.ru/cart",
            result={
                "provider": "yandex_api_adapter",
                "cart_cleared": False,
                "added_items": [
                    {
                        "product_name": "Спагетти",
                        "product_url": "https://eda.yandex.ru/product/sku-pasta-1",
                        "package_count": 1,
                        "added_package_count": 1,
                    }
                ],
            },
        )
        run.selected_attempt = attempt
        run.save(update_fields=["selected_attempt"])

        with self.assertRaises(CartAgentError) as caught:
            process_cart_cleanup(run)

        self.assertTrue(caught.exception.mutation_possible)
        cleanup.assert_not_called()

    @patch("recipes.carting.pipeline.cleanup_store_cart")
    def test_uncertain_cleanup_failure_requires_manual_check(self, cleanup):
        run = self.make_run()
        run.status = CartRun.Status.CLEANING
        run.cleanup_requested_at = timezone.now()
        run.save(update_fields=["status", "cleanup_requested_at"])
        attempt = CartAttempt.objects.create(
            run=run,
            store="auchan",
            status=CartAttempt.Status.EXACT,
            result={
                "cart_cleared": False,
                "added_items": [
                    {
                        "product_name": "Спагетти",
                        "product_url": "https://eda.yandex.ru/product/sku-pasta-1",
                        "package_count": 2,
                    }
                ],
            },
        )
        run.selected_attempt = attempt
        run.save(update_fields=["selected_attempt"])
        cleanup.return_value = {"status": "failed", "summary": "Связь оборвалась"}

        with self.assertRaises(CartAgentError) as caught:
            process_cart_cleanup(run)

        self.assertTrue(caught.exception.mutation_possible)

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

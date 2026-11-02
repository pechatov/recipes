from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from recipes.models import CartAttempt, CartItemMatch, CartRun
from recipes.locking import CART_BROWSER_LOCK, acquire_application_lock

from .client import (
    CartAgentError,
    assemble_store_cart,
    cleanup_store_cart,
    compare_stores,
    match_search_results,
)
from .coordination import (
    browser_login_session_blocks_worker,
    reconcile_expired_browser_login_sessions,
)


AGENT_STATUSES = {
    CartAttempt.Status.EXACT,
    CartAttempt.Status.SUBSTITUTIONS,
    CartAttempt.Status.INCOMPLETE,
    CartAttempt.Status.LOGIN_REQUIRED,
    CartAttempt.Status.BLOCKED,
    CartAttempt.Status.FAILED,
}
MATCH_QUALITIES = {
    CartItemMatch.MatchQuality.EXACT,
    CartItemMatch.MatchQuality.SUBSTITUTE,
    CartItemMatch.MatchQuality.MISSING,
}
PRODUCT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,128}")
# Яндекс Еда storefronts live on eda.yandex.ru; Яндекс Лавка is a separate
# site with /good/<id> product pages.
SAFE_CART_HOSTS = {"eda.yandex.ru", "lavka.yandex.ru"}
PRODUCT_PATH_MARKERS = {"product", "good"}


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _safe_yandex_food_url(value) -> str:
    url = _text(value, 2048)
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.hostname not in SAFE_CART_HOSTS:
        return ""
    return url


def _yandex_food_product_id(value) -> str:
    url = _safe_yandex_food_url(value)
    if not url:
        return ""
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) < 2 or parts[-2] not in PRODUCT_PATH_MARKERS:
        return ""
    product_id = parts[-1]
    return product_id if PRODUCT_ID_PATTERN.fullmatch(product_id) else ""


# The adapter signs comparison results for an hour; leave a margin for the
# worker to pick the chosen store up and reach the apply call.
COMPARISON_LIFETIME = timedelta(minutes=50)


def maximum_browser_operation_seconds() -> int:
    """Longest a single remote browser call may legitimately run."""
    return max(
        settings.CART_AI_TIMEOUT_SECONDS,
        settings.CART_ADAPTER_TIMEOUT_SECONDS,
        settings.CART_COMPARE_TIMEOUT_SECONDS + 60,
    ) + 60


def _price(value) -> Decimal | None:
    try:
        price = Decimal(str(value or "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not price.is_finite() or price <= 0 or price >= Decimal("100000000"):
        return None
    return price.quantize(Decimal("0.01"))


def matches_total_price(matches) -> tuple[Decimal | None, bool]:
    """Sum price × packages over found items; report whether every price was known."""
    total = Decimal("0")
    complete = True
    found = False
    for match in matches:
        if _text(match.get("quality"), 16) == CartItemMatch.MatchQuality.MISSING:
            continue
        found = True
        price = _price(match.get("price"))
        count = _package_count(match.get("package_count"))
        if price is None or count < 1:
            complete = False
            continue
        total += price * count
    if not found:
        return None, True
    return total, complete


def _package_count(value, *, maximum: int = 100) -> int:
    try:
        return max(0, min(int(value or 0), maximum))
    except (TypeError, ValueError):
        return 0


def _save_result(attempt: CartAttempt, data: dict) -> None:
    raw_status = _text(data.get("status"), 24)
    status = raw_status if raw_status in AGENT_STATUSES else CartAttempt.Status.FAILED
    expected = [item["name"] for item in attempt.run.ingredient_snapshot]
    reported_items = data.get("items", [])
    if not isinstance(reported_items, list):
        reported_items = []
    by_name = {}
    for item in reported_items:
        if isinstance(item, dict):
            by_name.setdefault(_text(item.get("ingredient_name"), 180).casefold(), item)

    attempt.matches.all().delete()
    matches = []
    added_by_product = {}
    for order, ingredient_name in enumerate(expected):
        item = by_name.get(ingredient_name.casefold(), {})
        quality = _text(item.get("quality"), 16)
        if quality not in MATCH_QUALITIES:
            quality = CartItemMatch.MatchQuality.MISSING
        package_count = _package_count(item.get("package_count"))
        product_name = _text(item.get("product_name"), 300)
        product_url = _safe_yandex_food_url(item.get("product_url"))
        product_id = _yandex_food_product_id(product_url)
        matches.append(
            CartItemMatch(
                attempt=attempt,
                ingredient_name=ingredient_name,
                requested_quantity=_text(item.get("requested_quantity"), 80),
                product_name=product_name,
                product_url=product_url,
                package_count=package_count,
                price=(
                    _price(item.get("price"))
                    if quality != CartItemMatch.MatchQuality.MISSING
                    else None
                ),
                quality=quality,
                warning=_text(item.get("warning"), 500),
                order=order,
            )
        )

        # The cleanup journal must only contain additions tied to a requested
        # ingredient. Never trust the agent's free-form top-level added_items:
        # page content can influence the browser agent and make it name a
        # pre-existing user product. The per-ingredient addition is bounded by
        # the validated total package count for that same match.
        added_package_count = _package_count(
            item.get("added_package_count"),
            maximum=package_count,
        )
        if (
            added_package_count
            and product_name
            and quality != CartItemMatch.MatchQuality.MISSING
        ):
            key = product_id or product_url or product_name.casefold()
            existing = added_by_product.setdefault(
                key,
                {
                    "product_name": product_name,
                    "product_url": product_url,
                    "product_id": product_id,
                    "package_count": 0,
                    "added_package_count": 0,
                },
            )
            added_total = min(
                100,
                existing["added_package_count"] + added_package_count,
            )
            # package_count is retained for old Hermes cleanup prompts. The
            # explicit field prevents adapter cleanup from confusing the
            # recipe target with the quantity actually added by this run.
            existing["added_package_count"] = added_total
            existing["package_count"] = added_total
    CartItemMatch.objects.bulk_create(matches)

    qualities = {match.quality for match in matches}
    if status == CartAttempt.Status.EXACT and (
        CartItemMatch.MatchQuality.MISSING in qualities
        or any(match.package_count < 1 for match in matches)
    ):
        status = CartAttempt.Status.INCOMPLETE
    elif status == CartAttempt.Status.EXACT and CartItemMatch.MatchQuality.SUBSTITUTE in qualities:
        status = CartAttempt.Status.SUBSTITUTIONS
    elif status == CartAttempt.Status.EXACT and qualities != {CartItemMatch.MatchQuality.EXACT}:
        status = CartAttempt.Status.INCOMPLETE
    elif CartItemMatch.MatchQuality.MISSING in qualities and status in {
        CartAttempt.Status.EXACT,
        CartAttempt.Status.SUBSTITUTIONS,
    }:
        status = CartAttempt.Status.INCOMPLETE

    added_items = list(added_by_product.values())

    result = dict(data)
    result["added_items"] = added_items
    result["cart_mutated"] = bool(added_items)
    result["cart_cleared"] = bool(data.get("cart_cleared"))
    if status == CartAttempt.Status.EXACT and result["cart_cleared"]:
        status = CartAttempt.Status.INCOMPLETE

    cart_url = (
        "" if result["cart_cleared"] else _safe_yandex_food_url(data.get("cart_url"))
    )
    if status in {CartAttempt.Status.EXACT, CartAttempt.Status.SUBSTITUTIONS} and not cart_url:
        status = CartAttempt.Status.FAILED
        result["validation_error"] = "missing_safe_cart_url"

    total_price, price_complete = matches_total_price(
        [
            {
                "quality": match.quality,
                "price": match.price,
                "package_count": match.package_count,
            }
            for match in matches
        ]
    )
    result["price_complete"] = price_complete

    attempt.status = status
    attempt.cart_url = cart_url
    attempt.summary = _text(data.get("summary"), 500)
    attempt.result = result
    attempt.total_price = total_price
    attempt.finished_at = timezone.now()
    attempt.save(
        update_fields=[
            "status",
            "cart_url",
            "summary",
            "result",
            "total_price",
            "finished_at",
        ]
    )


def attempt_added_items(attempt: CartAttempt | None) -> list[dict]:
    if not attempt or not isinstance(attempt.result, dict):
        return []
    if attempt.result.get("cart_cleared"):
        return []
    items = attempt.result.get("added_items")
    return items if isinstance(items, list) else []


def attempt_needs_cleanup(attempt: CartAttempt | None) -> bool:
    return bool(attempt_added_items(attempt))


def finish_requested_cart_stop(
    run: CartRun,
    *,
    attempt: CartAttempt | None = None,
    mutation_unknown: bool = False,
) -> bool:
    """Finish a cooperative stop after any in-flight browser call has returned."""
    with transaction.atomic():
        locked_run = CartRun.objects.select_for_update().get(pk=run.pk)
        if not locked_run.cancellation_requested_at:
            run.cancellation_requested_at = None
            return False
        if (
            locked_run.status == CartRun.Status.MANUAL_CHECK
            and locked_run.browser_operation_started_at
        ):
            run.refresh_from_db()
            return True
        attempt = (
            attempt
            or locked_run.selected_attempt
            or latest_cart_attempt(locked_run)
        )
        attempt_result = (
            attempt.result if attempt and isinstance(attempt.result, dict) else {}
        )
        additions_may_remain = bool(
            mutation_unknown
            or attempt_result.get("mutation_unknown")
            or attempt_needs_cleanup(attempt)
            or (
                not attempt
                and locked_run.cleanup_requested_at
                and not locked_run.cleaned_at
            )
        )
        if locked_run.status == CartRun.Status.CANCELLED:
            fields = ["browser_operation_started_at"]
            locked_run.browser_operation_started_at = None
            if additions_may_remain:
                locked_run.selected_attempt = attempt or locked_run.selected_attempt
                locked_run.cleaned_at = None
                locked_run.error = (
                    "Сборка остановлена по вашему запросу. В Яндекс Еде могли "
                    "остаться товары этой попытки — проверьте корзину перед новым запуском."
                )
                fields.extend(["selected_attempt", "cleaned_at", "error"])
            locked_run.save(update_fields=fields)
            run.refresh_from_db()
            return True

        now = timezone.now()
        locked_run.status = CartRun.Status.CANCELLED
        locked_run.selected_attempt = attempt or locked_run.selected_attempt
        locked_run.finished_at = now
        locked_run.confirmation_deadline = None
        locked_run.cleanup_requested_at = None
        locked_run.browser_operation_started_at = None
        if additions_may_remain:
            locked_run.cleaned_at = None
            locked_run.error = (
                "Сборка остановлена по вашему запросу. В Яндекс Еде могли "
                "остаться товары этой попытки — проверьте корзину перед новым запуском."
            )
        else:
            locked_run.cleaned_at = now
            locked_run.error = ""
        locked_run.save(
            update_fields=[
                "status",
                "selected_attempt",
                "finished_at",
                "confirmation_deadline",
                "cleanup_requested_at",
                "browser_operation_started_at",
                "cleaned_at",
                "error",
            ]
        )
    run.refresh_from_db()
    return True


def latest_cart_attempt(run: CartRun, queryset=None):
    """The attempt that may have touched a cart.

    In a comparison run every store has an attempt, but only the chosen store
    is ever assembled.
    """
    attempts = queryset if queryset is not None else CartAttempt.objects.all()
    attempts = attempts.filter(run=run)
    if run.chosen_store:
        return attempts.filter(store=run.chosen_store).first()
    if run.comparison:
        return None
    return attempts.order_by("-started_at").first()


def _reserve_browser_operation(run: CartRun) -> bool:
    """Serialize an external browser dispatch against a concurrent stop request."""
    with transaction.atomic():
        locked_run = CartRun.objects.select_for_update().get(pk=run.pk)
        if locked_run.cancellation_requested_at or locked_run.status not in {
            CartRun.Status.PROCESSING,
            CartRun.Status.CLEANING,
        }:
            run.cancellation_requested_at = locked_run.cancellation_requested_at
            run.status = locked_run.status
            return False
        locked_run.browser_operation_started_at = timezone.now()
        locked_run.save(update_fields=["browser_operation_started_at"])
        run.browser_operation_started_at = locked_run.browser_operation_started_at
    return True


def _release_browser_operation(run: CartRun) -> None:
    CartRun.objects.filter(
        pk=run.pk,
        browser_operation_started_at=run.browser_operation_started_at,
    ).update(browser_operation_started_at=None)
    run.browser_operation_started_at = None


def _browser_operation_is_current(run: CartRun) -> bool:
    if not run.browser_operation_started_at:
        return False
    current = CartRun.objects.filter(
        pk=run.pk,
        status__in=[CartRun.Status.PROCESSING, CartRun.Status.CLEANING],
        browser_operation_started_at=run.browser_operation_started_at,
    ).exists()
    if not current:
        run.refresh_from_db()
    return current


def record_unexpected_browser_failure(
    run: CartRun,
    *,
    attempt: CartAttempt | None = None,
    error: Exception | None = None,
) -> bool:
    """Durably release a reservation into a user-resolvable safety state."""
    with transaction.atomic():
        locked_run = CartRun.objects.select_for_update().get(pk=run.pk)
        if not locked_run.browser_operation_started_at:
            run.browser_operation_started_at = None
            return False
        attempt_id = attempt.pk if attempt else locked_run.selected_attempt_id
        locked_attempt = None
        if attempt_id:
            locked_attempt = CartAttempt.objects.select_for_update().filter(
                pk=attempt_id,
                run=locked_run,
            ).first()
        if not locked_attempt:
            locked_attempt = latest_cart_attempt(
                locked_run, CartAttempt.objects.select_for_update()
            )
        diagnostic = _text(error, 500)
        if locked_attempt:
            result = dict(locked_attempt.result or {})
            result["mutation_unknown"] = True
            if diagnostic:
                result["error"] = diagnostic
            locked_attempt.status = CartAttempt.Status.FAILED
            locked_attempt.summary = diagnostic or "Внутренняя ошибка browser-операции."
            locked_attempt.result = result
            locked_attempt.finished_at = timezone.now()
            locked_attempt.save(
                update_fields=["status", "summary", "result", "finished_at"]
            )
        locked_run.status = CartRun.Status.MANUAL_CHECK
        locked_run.selected_attempt = locked_attempt or locked_run.selected_attempt
        locked_run.browser_operation_started_at = None
        locked_run.finished_at = timezone.now()
        locked_run.confirmation_deadline = None
        locked_run.error = (
            "Операция с Яндекс Едой завершилась неожиданно. Проверьте корзину "
            "вручную перед следующим запуском."
        )
        locked_run.save(
            update_fields=[
                "status",
                "selected_attempt",
                "browser_operation_started_at",
                "finished_at",
                "confirmation_deadline",
                "error",
            ]
        )
    run.refresh_from_db()
    if attempt:
        attempt.refresh_from_db()
    return True


def _mark_attempt_cleaned(attempt: CartAttempt, summary: str = "") -> None:
    result = dict(attempt.result or {})
    result["cart_cleared"] = True
    result["cleanup_summary"] = _text(summary, 500)
    attempt.result = result
    attempt.cart_url = ""
    attempt.save(update_fields=["result", "cart_url"])


def _finish_ready_run(run: CartRun, attempt: CartAttempt) -> None:
    now = timezone.now()
    run.status = (
        CartRun.Status.COMPLETED
        if attempt.status == CartAttempt.Status.EXACT
        else CartRun.Status.REVIEW
    )
    run.selected_attempt = attempt
    run.finished_at = now
    run.confirmation_deadline = (
        now + timedelta(minutes=settings.CART_CONFIRMATION_MINUTES)
        if attempt_needs_cleanup(attempt)
        else None
    )
    run.error = ""
    run.save(
        update_fields=[
            "status",
            "selected_attempt",
            "finished_at",
            "confirmation_deadline",
            "error",
        ]
    )


def _cleanup_attempt(run: CartRun, attempt: CartAttempt) -> str:
    items = attempt_added_items(attempt)
    if not items:
        return "cleared"
    safe_items = []
    for item in items:
        if not isinstance(item, dict):
            raise CartAgentError(
                "Безопасная очистка невозможна: журнал добавлений повреждён."
            )
        product_name = _text(item.get("product_name"), 300)
        product_url = _safe_yandex_food_url(item.get("product_url"))
        product_id = _yandex_food_product_id(product_url)
        try:
            package_count = int(
                item.get("added_package_count", item.get("package_count")) or 0
            )
        except (TypeError, ValueError):
            package_count = 0
        if not product_name or not product_id or not 1 <= package_count <= 100:
            raise CartAgentError(
                "Автоматическая очистка остановлена: товар нельзя однозначно "
                "сопоставить со строкой корзины."
            )
        safe_items.append(
            {
                "product_name": product_name,
                "product_url": product_url,
                "product_id": product_id,
                "package_count": package_count,
                "added_package_count": package_count,
            }
        )
    result = attempt.result if isinstance(attempt.result, dict) else {}
    cleanup_token = _text(result.get("cleanup_token"), 60_000)
    if result.get("provider") == "yandex_api_adapter" and not cleanup_token:
        raise CartAgentError(
            "Подписанный журнал адаптерной сборки отсутствует; проверьте "
            "корзину вручную.",
            mutation_possible=True,
        )
    if not _reserve_browser_operation(run):
        finish_requested_cart_stop(run, attempt=attempt)
        return "stopped"
    try:
        data = cleanup_store_cart(
            run,
            attempt.store,
            safe_items,
            attempt.cart_url,
            cleanup_token=cleanup_token,
        )
    except CartAgentError as error:
        if not _browser_operation_is_current(run):
            return "manual_check"
        try:
            if error.mutation_possible:
                result = dict(attempt.result or {})
                result["mutation_unknown"] = True
                result["operation_uncertain"] = error.operation_uncertain
                attempt.result = result
                attempt.save(update_fields=["result"])
        except Exception as persistence_error:
            record_unexpected_browser_failure(
                run,
                attempt=attempt,
                error=persistence_error,
            )
            raise CartAgentError(
                "Не удалось сохранить результат browser-операции.",
                mutation_possible=True,
            ) from persistence_error
        if error.operation_uncertain:
            run.status = CartRun.Status.MANUAL_CHECK
            run.selected_attempt = attempt
            run.finished_at = timezone.now()
            run.error = (
                "Связь с browser controller оборвалась во время очистки. "
                "Проверьте корзину вручную."
            )
            run.save(
                update_fields=["status", "selected_attempt", "finished_at", "error"]
            )
            return "manual_check"
        _release_browser_operation(run)
        raise
    except Exception as error:
        if not _browser_operation_is_current(run):
            return "stopped"
        record_unexpected_browser_failure(run, attempt=attempt, error=error)
        raise CartAgentError(
            "Browser-операция завершилась неожиданно.",
            mutation_possible=True,
        ) from error
    if not _browser_operation_is_current(run):
        return "stopped"
    try:
        status = _text(data.get("status"), 24)
        if status == "cleared":
            _mark_attempt_cleaned(attempt, data.get("summary", ""))
        elif data.get("mutation_possible"):
            result = dict(attempt.result or {})
            result["mutation_unknown"] = True
            attempt.result = result
            attempt.save(update_fields=["result"])
    except Exception as error:
        record_unexpected_browser_failure(run, attempt=attempt, error=error)
        raise CartAgentError(
            "Не удалось сохранить результат browser-операции.",
            mutation_possible=True,
        ) from error
    # As with assembly, publish an idle run only after the cleanup outcome is
    # durable so a concurrent stop cannot make a decision from stale journal data.
    _release_browser_operation(run)
    return status


def _record_outstanding_cleanup(run: CartRun, attempt: CartAttempt, error: str) -> None:
    run.selected_attempt = attempt
    run.cleanup_requested_at = timezone.now()
    run.error = error
    run.save(update_fields=["selected_attempt", "cleanup_requested_at", "error"])


def _is_clean_store_unavailable(attempt: CartAttempt) -> bool:
    result = attempt.result if isinstance(attempt.result, dict) else {}
    return (
        attempt.status == CartAttempt.Status.FAILED
        and result.get("reason") == "store_unavailable"
        and result.get("cart_cleared") is True
        and result.get("cart_mutated") is False
        and result.get("items") == []
        and not attempt_needs_cleanup(attempt)
    )


def _finish_store_unavailable_run(
    run: CartRun,
    attempt: CartAttempt,
    *,
    error: str = "",
) -> None:
    run.status = CartRun.Status.FAILED
    run.selected_attempt = attempt
    run.finished_at = timezone.now()
    run.confirmation_deadline = None
    run.error = error or attempt.summary or (
        "Выбранный магазин недоступен для доставки по сохранённому адресу."
    )
    run.save(
        update_fields=[
            "status",
            "selected_attempt",
            "finished_at",
            "confirmation_deadline",
            "error",
        ]
    )


def _mark_inconsistent_store_unavailable_for_manual_check(
    run: CartRun,
    attempt: CartAttempt,
) -> None:
    result = dict(attempt.result or {})
    result["validation_error"] = "inconsistent_store_unavailable_result"
    result["mutation_unknown"] = True
    attempt.status = CartAttempt.Status.FAILED
    attempt.result = result
    attempt.save(update_fields=["status", "result"])

    run.status = CartRun.Status.MANUAL_CHECK
    run.selected_attempt = attempt
    run.finished_at = timezone.now()
    run.confirmation_deadline = None
    run.error = (
        "Агент сообщил, что магазин недоступен, но не подтвердил отсутствие "
        "изменений корзины. Проверьте корзину вручную перед повтором."
    )
    run.save(
        update_fields=[
            "status",
            "selected_attempt",
            "finished_at",
            "confirmation_deadline",
            "error",
        ]
    )


def _best_attempt(run: CartRun):
    candidates = list(
        run.attempts.exclude(
            status__in=[
                CartAttempt.Status.PROCESSING,
                CartAttempt.Status.LOGIN_REQUIRED,
                CartAttempt.Status.BLOCKED,
                CartAttempt.Status.FAILED,
            ]
        ).prefetch_related("matches")
    )
    if not candidates:
        return None

    def score(attempt):
        matches = list(attempt.matches.all())
        exact = sum(match.quality == CartItemMatch.MatchQuality.EXACT for match in matches)
        substitutes = sum(
            match.quality == CartItemMatch.MatchQuality.SUBSTITUTE for match in matches
        )
        return exact * 100 + substitutes * 10 - attempt.started_at.timestamp() / 10**12

    return max(candidates, key=score)


COMPARISON_FAILURES = {
    "login_required": (CartAttempt.Status.LOGIN_REQUIRED, "Нужно войти в Яндекс."),
    "blocked": (CartAttempt.Status.BLOCKED, "Яндекс запросил ручную проверку."),
    "incomplete": (CartAttempt.Status.FAILED, "Магазин недоступен по сохранённому адресу."),
    "timeout": (CartAttempt.Status.FAILED, "Магазин не ответил вовремя."),
    "failed": (CartAttempt.Status.FAILED, "Магазин не удалось проверить."),
}


def comparison_counts(matches) -> dict[str, int]:
    counts = {quality: 0 for quality in MATCH_QUALITIES}
    for match in matches:
        quality = _text(match.get("quality"), 16)
        if quality in counts:
            counts[quality] += 1
    return counts


def _save_comparison(run: CartRun, attempt: CartAttempt, outcome: dict) -> None:
    status = _text(outcome.get("status"), 24)
    attempt.matches.all().delete()
    attempt.cart_url = ""
    attempt.total_price = None
    attempt.finished_at = timezone.now()
    result = {"provider": "yandex_api_adapter", "comparison_outcome": status}
    matches = None
    if status == "ready":
        try:
            matches = match_search_results(run, outcome)
        except CartAgentError as error:
            status = "failed"
            result["comparison_outcome"] = status
            result["error"] = _text(error, 500)
        selection_token = _text(outcome.get("selection_token"), 60_000)
        if matches is not None and not selection_token:
            matches = None
            status = "failed"
            result["comparison_outcome"] = status
    if matches is None:
        attempt.status, default_summary = COMPARISON_FAILURES.get(
            status, COMPARISON_FAILURES["failed"]
        )
        attempt.summary = _text(outcome.get("summary"), 500) or default_summary
        attempt.result = result
        attempt.save(
            update_fields=[
                "status",
                "summary",
                "result",
                "cart_url",
                "total_price",
                "finished_at",
            ]
        )
        return

    counts = comparison_counts(matches)
    if counts[CartItemMatch.MatchQuality.MISSING]:
        attempt.status = CartAttempt.Status.INCOMPLETE
    elif counts[CartItemMatch.MatchQuality.SUBSTITUTE]:
        attempt.status = CartAttempt.Status.SUBSTITUTIONS
    else:
        attempt.status = CartAttempt.Status.EXACT
    total_price, price_complete = matches_total_price(matches)
    attempt.total_price = total_price
    attempt.summary = (
        f"Точно: {counts['exact']}, замены: {counts['substitute']}, "
        f"не найдено: {counts['missing']}."
    )
    result.update({"counts": counts, "price_complete": price_complete})
    attempt.result = result
    attempt.comparison = {
        "selection_token": selection_token,
        "cart_url": _safe_yandex_food_url(outcome.get("cart_url")),
        "elapsed_ms": outcome.get("elapsed_ms"),
        "searched_at": timezone.now().isoformat(),
        "matches": matches,
    }
    attempt.save(
        update_fields=[
            "comparison",
            "status",
            "summary",
            "result",
            "cart_url",
            "total_price",
            "finished_at",
        ]
    )
    CartItemMatch.objects.bulk_create(
        [
            CartItemMatch(
                attempt=attempt,
                ingredient_name=_text(match.get("ingredient_name"), 180),
                requested_quantity=_text(match.get("requested_quantity"), 80),
                product_name=_text(match.get("product_name"), 300),
                product_url=_safe_yandex_food_url(match.get("product_url")),
                package_count=_package_count(match.get("package_count")),
                price=(
                    _price(match.get("price"))
                    if match.get("quality") != CartItemMatch.MatchQuality.MISSING
                    else None
                ),
                quality=match.get("quality"),
                warning=_text(match.get("warning"), 500),
                order=order,
            )
            for order, match in enumerate(matches)
        ]
    )


def comparison_selection(attempt: CartAttempt | None) -> dict | None:
    """The signed comparison of one store, if it can still be applied."""
    if not attempt:
        return None
    comparison = attempt.comparison
    if not isinstance(comparison, dict):
        return None
    try:
        searched_at = datetime.fromisoformat(str(comparison.get("searched_at") or ""))
    except ValueError:
        return None
    if timezone.is_naive(searched_at) or timezone.now() - searched_at > COMPARISON_LIFETIME:
        return None
    matches = comparison.get("matches")
    if (
        not comparison.get("selection_token")
        or not isinstance(matches, list)
        or len(matches) != len(attempt.run.ingredient_snapshot)
        or not any(
            isinstance(match, dict) and match.get("quality") != "missing"
            for match in matches
        )
    ):
        return None
    return comparison


def comparison_is_fresh(attempt: CartAttempt | None) -> bool:
    return comparison_selection(attempt) is not None


def _finish_stale_choice(run: CartRun) -> None:
    run.status = CartRun.Status.FAILED
    run.finished_at = timezone.now()
    run.error = (
        "Результат сравнения устарел: цены и наличие могли измениться. "
        "Корзина не менялась — проверьте магазины заново."
    )
    run.save(update_fields=["status", "finished_at", "error"])


def process_cart_comparison(run: CartRun) -> None:
    """Search every store at once; never touch a cart."""
    if finish_requested_cart_stop(run):
        return
    stores = list(run.store_priority)
    attempts = {}
    for store in stores:
        attempt, _ = CartAttempt.objects.update_or_create(
            run=run,
            store=store,
            defaults={
                "status": CartAttempt.Status.PROCESSING,
                "cart_url": "",
                "summary": "",
                "result": {},
                "comparison": {},
                "total_price": None,
                "finished_at": None,
            },
        )
        attempt.matches.all().delete()
        attempts[store] = attempt
    if not _reserve_browser_operation(run):
        finish_requested_cart_stop(run)
        return
    try:
        outcomes = compare_stores(run, stores)
    except Exception as error:
        if not _browser_operation_is_current(run):
            return
        # The comparison never changes a cart, so a failure leaves nothing
        # to clean up: release the reservation and let the user retry.
        _release_browser_operation(run)
        if finish_requested_cart_stop(run):
            return
        message = (
            _text(error, 500)
            if isinstance(error, CartAgentError)
            else "Сравнение магазинов завершилось неожиданно."
        )
        CartAttempt.objects.filter(
            run=run, status=CartAttempt.Status.PROCESSING
        ).update(
            status=CartAttempt.Status.FAILED,
            summary=message,
            finished_at=timezone.now(),
        )
        run.status = CartRun.Status.FAILED
        run.error = message or "Сравнение магазинов не завершилось."
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "error", "finished_at"])
        if not isinstance(error, CartAgentError):
            raise
        return
    if not _browser_operation_is_current(run):
        return
    try:
        for store in stores:
            _save_comparison(run, attempts[store], outcomes.get(store) or {})
    finally:
        _release_browser_operation(run)
    if finish_requested_cart_stop(run):
        return

    statuses = {attempt.status for attempt in attempts.values()}
    found = statuses & {
        CartAttempt.Status.EXACT,
        CartAttempt.Status.SUBSTITUTIONS,
        CartAttempt.Status.INCOMPLETE,
    }
    run.finished_at = timezone.now()
    run.selected_attempt = None
    if found:
        run.status = CartRun.Status.CHOOSING
        run.error = ""
    elif statuses & {CartAttempt.Status.LOGIN_REQUIRED, CartAttempt.Status.BLOCKED}:
        run.status = CartRun.Status.LOGIN_REQUIRED
        run.error = "Яндекс попросил войти или пройти проверку в браузере."
    else:
        run.status = CartRun.Status.FAILED
        run.error = "Ни один магазин не удалось проверить."
    run.save(update_fields=["status", "error", "finished_at", "selected_attempt"])


def process_cart_run(run: CartRun) -> None:
    if run.is_comparing:
        process_cart_comparison(run)
        return
    while run.next_store_index < len(run.store_priority):
        if finish_requested_cart_stop(run):
            return
        store = run.store_priority[run.next_store_index]
        prepared = None
        if run.chosen_store == store:
            prepared = comparison_selection(
                run.attempts.filter(store=store).select_related("run").first()
            )
            if prepared is None:
                # Never fall back to a fresh search: it could add products
                # the user has not reviewed. Nothing has touched the cart.
                _finish_stale_choice(run)
                return
        attempt, _ = CartAttempt.objects.update_or_create(
            run=run,
            store=store,
            defaults={
                "status": CartAttempt.Status.PROCESSING,
                "cart_url": "",
                "summary": "",
                "result": {},
                "total_price": None,
                "finished_at": None,
            },
        )
        if not _reserve_browser_operation(run):
            finish_requested_cart_stop(run, attempt=attempt)
            return
        try:
            data = assemble_store_cart(run, store, prepared)
        except CartAgentError as error:
            if not _browser_operation_is_current(run):
                return
            diagnostic = _text(str(error), 500)
            attempt.status = CartAttempt.Status.FAILED
            attempt.summary = diagnostic or "Сервис не завершил одноэтапную сборку."
            attempt.result = {
                "mutation_unknown": error.mutation_possible,
                "operation_uncertain": error.operation_uncertain,
                "error": diagnostic,
            }
            attempt.finished_at = timezone.now()
            attempt.save(update_fields=["status", "summary", "result", "finished_at"])
            if error.operation_uncertain:
                run.status = CartRun.Status.MANUAL_CHECK
                run.selected_attempt = attempt
                run.finished_at = timezone.now()
                run.error = (
                    "Связь с browser controller оборвалась. Удалённая операция "
                    "могла продолжить работу — проверьте корзину вручную."
                )
                run.save(
                    update_fields=["status", "selected_attempt", "finished_at", "error"]
                )
                return
            # Keep the reservation until the mutation outcome is durable. A
            # concurrent stop can now safely derive its warning from attempt.
            _release_browser_operation(run)
            if finish_requested_cart_stop(
                run,
                attempt=attempt,
                mutation_unknown=error.mutation_possible,
            ):
                return
            if error.mutation_possible:
                run.status = CartRun.Status.MANUAL_CHECK
                run.selected_attempt = attempt
                run.finished_at = timezone.now()
                run.error = (
                    "Связь с браузером оборвалась после запуска задачи. "
                    "Проверьте корзину вручную перед повтором."
                )
                run.save(
                    update_fields=[
                        "status",
                        "selected_attempt",
                        "finished_at",
                        "error",
                    ]
                )
                return
            raise
        except Exception as error:
            if not _browser_operation_is_current(run):
                return
            record_unexpected_browser_failure(run, attempt=attempt, error=error)
            raise CartAgentError(
                "Browser-операция завершилась неожиданно.",
                mutation_possible=True,
            ) from error
        if not _browser_operation_is_current(run):
            return
        try:
            _save_result(attempt, data)
        except Exception as error:
            record_unexpected_browser_failure(run, attempt=attempt, error=error)
            raise CartAgentError(
                "Не удалось сохранить результат browser-операции.",
                mutation_possible=True,
            ) from error
        # The result journal must be committed before cart_stop can observe an
        # idle run; otherwise it can incorrectly claim that no additions remain.
        _release_browser_operation(run)
        if finish_requested_cart_stop(run, attempt=attempt):
            return
        run.next_store_index += 1
        run.save(update_fields=["next_store_index"])

        if data.get("reason") == "store_unavailable":
            if _is_clean_store_unavailable(attempt):
                _finish_store_unavailable_run(run, attempt)
                return
            if attempt_needs_cleanup(attempt):
                try:
                    cleanup_status = _cleanup_attempt(run, attempt)
                except CartAgentError:
                    _record_outstanding_cleanup(
                        run,
                        attempt,
                        "Не удалось очистить товары после противоречивого "
                        "ответа о недоступности магазина.",
                    )
                    raise
                if cleanup_status in {"stopped", "manual_check"}:
                    return
                if cleanup_status != "cleared":
                    _record_outstanding_cleanup(
                        run,
                        attempt,
                        "Нужно завершить очистку после противоречивого ответа "
                        "о недоступности магазина.",
                    )
                    raise CartAgentError(
                        "Агент не смог подтвердить полную очистку после "
                        "противоречивого ответа о недоступности магазина.",
                        mutation_possible=True,
                    )
                cleaned_result = dict(attempt.result or {})
                cleaned_result["validation_error"] = (
                    "inconsistent_store_unavailable_result"
                )
                cleaned_result["mutation_unknown"] = False
                attempt.status = CartAttempt.Status.FAILED
                attempt.result = cleaned_result
                attempt.save(update_fields=["status", "result"])
                _finish_store_unavailable_run(
                    run,
                    attempt,
                    error=(
                        "Агент сообщил противоречивые сведения о доступности "
                        "магазина. Добавленные товары удалены; повторите сборку."
                    ),
                )
                return
            _mark_inconsistent_store_unavailable_for_manual_check(run, attempt)
            return

        if attempt.status in {
            CartAttempt.Status.EXACT,
            CartAttempt.Status.SUBSTITUTIONS,
        }:
            _finish_ready_run(run, attempt)
            return
        if attempt.status == CartAttempt.Status.LOGIN_REQUIRED:
            run.status = CartRun.Status.LOGIN_REQUIRED
            run.next_store_index -= 1
            run.finished_at = timezone.now()
            if attempt_needs_cleanup(attempt):
                run.selected_attempt = attempt
                run.cleanup_requested_at = timezone.now()
            run.save(
                update_fields=[
                    "status",
                    "selected_attempt",
                    "next_store_index",
                    "finished_at",
                    "cleanup_requested_at",
                ]
            )
            return
        if attempt.status == CartAttempt.Status.BLOCKED:
            # CAPTCHA is recoverable by the account owner in the persistent
            # browser profile. Pause on this exact store so Retry continues
            # here after the human verification instead of skipping vendors.
            run.status = CartRun.Status.LOGIN_REQUIRED
            run.next_store_index -= 1
            run.finished_at = timezone.now()
            if attempt_needs_cleanup(attempt):
                run.selected_attempt = attempt
                run.cleanup_requested_at = timezone.now()
            run.error = "Сайт попросил ручное подтверждение в браузере."
            run.save(
                update_fields=[
                    "status",
                    "selected_attempt",
                    "next_store_index",
                    "finished_at",
                    "cleanup_requested_at",
                    "error",
                ]
            )
            return

        if attempt.status == CartAttempt.Status.FAILED:
            if attempt_needs_cleanup(attempt):
                try:
                    cleanup_status = _cleanup_attempt(run, attempt)
                except CartAgentError:
                    _record_outstanding_cleanup(
                        run,
                        attempt,
                        "Не удалось очистить товары после неудачной сборки.",
                    )
                    raise
                if cleanup_status in {"stopped", "manual_check"}:
                    return
                if cleanup_status != "cleared":
                    _record_outstanding_cleanup(
                        run,
                        attempt,
                        "Нужно завершить очистку после неудачной сборки.",
                    )
                    raise CartAgentError(
                        "Агент не смог подтвердить полную очистку после "
                        "неудачной сборки.",
                        mutation_possible=True,
                    )
            continue

        missing_count = attempt.matches.filter(
            quality=CartItemMatch.MatchQuality.MISSING
        ).count()
        cleanup_threshold = max(2, math.ceil(len(run.ingredient_snapshot) * 0.25))
        if (
            attempt_needs_cleanup(attempt)
            and missing_count >= cleanup_threshold
            and not run.chosen_store
        ):
            try:
                cleanup_status = _cleanup_attempt(run, attempt)
            except CartAgentError:
                _record_outstanding_cleanup(
                    run,
                    attempt,
                    "Не удалось очистить неполную корзину.",
                )
                raise
            if cleanup_status in {"stopped", "manual_check"}:
                return
            if cleanup_status != "cleared":
                _record_outstanding_cleanup(
                    run,
                    attempt,
                    "Не удалось безопасно очистить неполную корзину.",
                )
                raise CartAgentError(
                    "Агент не смог подтвердить полную очистку неполной корзины.",
                    mutation_possible=True,
                )

        if attempt_needs_cleanup(attempt):
            _finish_ready_run(run, attempt)
            return

    if run.chosen_store:
        # Other stores' attempts are comparison results, not carts.
        best = run.attempts.filter(store=run.chosen_store).exclude(
            status=CartAttempt.Status.PROCESSING
        ).first()
    else:
        best = _best_attempt(run)
    run.selected_attempt = best
    run.finished_at = timezone.now()
    run.confirmation_deadline = None
    if best and best.status not in {CartAttempt.Status.BLOCKED, CartAttempt.Status.FAILED}:
        run.status = CartRun.Status.REVIEW
        run.error = ""
    else:
        run.status = CartRun.Status.FAILED
        run.error = "Ни один магазин не удалось проверить автоматически."
    run.save(
        update_fields=[
            "status",
            "selected_attempt",
            "finished_at",
            "confirmation_deadline",
            "error",
        ]
    )


def expire_unconfirmed_cart_runs() -> int:
    now = timezone.now()
    return CartRun.objects.filter(
        status__in=[CartRun.Status.COMPLETED, CartRun.Status.REVIEW],
        confirmation_deadline__isnull=False,
        confirmation_deadline__lte=now,
        confirmed_at__isnull=True,
    ).update(
        status=CartRun.Status.CLEANUP_PENDING,
        cleanup_requested_at=now,
        error="",
    )


def claim_cleanup_run():
    with transaction.atomic():
        acquire_application_lock(CART_BROWSER_LOCK)
        if not reconcile_expired_browser_login_sessions():
            return None
        users_requiring_manual_check = CartRun.objects.filter(
            status=CartRun.Status.MANUAL_CHECK
        ).values("requested_by_id")
        run = (
            CartRun.objects.select_for_update(skip_locked=True)
            .filter(status=CartRun.Status.CLEANUP_PENDING)
            .exclude(requested_by_id__in=users_requiring_manual_check)
            .order_by("cleanup_requested_at", "created_at")
            .first()
        )
        if not run:
            return None
        if browser_login_session_blocks_worker():
            return None
        if CartRun.objects.filter(
            browser_operation_started_at__isnull=False,
        ).exclude(pk=run.pk).exists():
            return None
        if CartRun.objects.filter(
            status__in=[CartRun.Status.PROCESSING, CartRun.Status.CLEANING]
        ).exclude(pk=run.pk).exists():
            return None
        run.status = CartRun.Status.CLEANING
        run.started_at = timezone.now()
        run.browser_operation_started_at = None
        run.error = ""
        run.save(
            update_fields=[
                "status",
                "started_at",
                "browser_operation_started_at",
                "error",
            ]
        )
        return run


def process_cart_cleanup(run: CartRun) -> None:
    if finish_requested_cart_stop(run):
        return
    attempt = run.selected_attempt
    if not attempt:
        raise CartAgentError("Для очистки не найден журнал добавленных товаров.")
    if not attempt_needs_cleanup(attempt):
        run.status = CartRun.Status.CANCELLED
        run.cleaned_at = timezone.now()
        run.confirmation_deadline = None
        run.error = ""
        run.save(
            update_fields=["status", "cleaned_at", "confirmation_deadline", "error"]
        )
        return

    status = _cleanup_attempt(run, attempt)
    if status == "manual_check":
        return
    if finish_requested_cart_stop(run, attempt=attempt):
        return
    if status != "cleared":
        raise CartAgentError(
            "Агент не смог подтвердить полную очистку корзины.",
            mutation_possible=True,
        )

    run.status = CartRun.Status.CANCELLED
    run.cleaned_at = timezone.now()
    run.confirmation_deadline = None
    run.finished_at = timezone.now()
    run.error = ""
    run.save(
        update_fields=[
            "status",
            "cleaned_at",
            "confirmation_deadline",
            "finished_at",
            "error",
        ]
    )


def claim_cart_run():
    with transaction.atomic():
        acquire_application_lock(CART_BROWSER_LOCK)
        if not reconcile_expired_browser_login_sessions():
            return None
        users_requiring_manual_check = CartRun.objects.filter(
            status=CartRun.Status.MANUAL_CHECK
        ).values("requested_by_id")
        run = (
            CartRun.objects.select_for_update(skip_locked=True)
            .filter(status=CartRun.Status.PENDING)
            .exclude(requested_by_id__in=users_requiring_manual_check)
            .order_by("created_at")
            .first()
        )
        if not run:
            return None
        # A human and the agent must never drive the shared Camofox process at
        # the same time. Even expired rows block until remote close is confirmed.
        if browser_login_session_blocks_worker():
            return None
        if CartRun.objects.filter(
            browser_operation_started_at__isnull=False,
        ).exclude(pk=run.pk).exists():
            return None
        # One persistent browser profile must not be shared by concurrent jobs.
        if CartRun.objects.filter(
            status__in=[CartRun.Status.PROCESSING, CartRun.Status.CLEANING]
        ).exclude(pk=run.pk).exists():
            return None
        # Never open another store while an earlier mutation still requires
        # cleanup, including a cleanup paused for login/CAPTCHA or failed safely.
        if CartRun.objects.filter(
            cleanup_requested_at__isnull=False,
            cleaned_at__isnull=True,
        ).exclude(pk=run.pk).exclude(status=CartRun.Status.CONFIRMED).exists():
            return None
        run.status = CartRun.Status.PROCESSING
        run.started_at = timezone.now()
        run.browser_operation_started_at = None
        run.finished_at = None
        run.error = ""
        run.save(
            update_fields=[
                "status",
                "started_at",
                "browser_operation_started_at",
                "finished_at",
                "error",
            ]
        )
        return run

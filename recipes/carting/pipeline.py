from __future__ import annotations

import math
import re
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from recipes.models import CartAttempt, CartItemMatch, CartRun
from recipes.locking import CART_BROWSER_LOCK, acquire_application_lock

from .client import CartAgentError, assemble_store_cart, cleanup_store_cart
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


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _safe_yandex_food_url(value) -> str:
    url = _text(value, 2048)
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.hostname != "eda.yandex.ru":
        return ""
    return url


def _yandex_food_product_id(value) -> str:
    url = _safe_yandex_food_url(value)
    if not url:
        return ""
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) < 2 or parts[-2] != "product":
        return ""
    product_id = parts[-1]
    return product_id if PRODUCT_ID_PATTERN.fullmatch(product_id) else ""


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

    attempt.status = status
    attempt.cart_url = cart_url
    attempt.summary = _text(data.get("summary"), 500)
    attempt.result = result
    attempt.finished_at = timezone.now()
    attempt.save(
        update_fields=["status", "cart_url", "summary", "result", "finished_at"]
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
    data = cleanup_store_cart(
        run,
        attempt.store,
        safe_items,
        attempt.cart_url,
        cleanup_token=cleanup_token,
    )
    status = _text(data.get("status"), 24)
    if status == "cleared":
        _mark_attempt_cleaned(attempt, data.get("summary", ""))
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


def process_cart_run(run: CartRun) -> None:
    while run.next_store_index < len(run.store_priority):
        store = run.store_priority[run.next_store_index]
        attempt, _ = CartAttempt.objects.update_or_create(
            run=run,
            store=store,
            defaults={
                "status": CartAttempt.Status.PROCESSING,
                "cart_url": "",
                "summary": "",
                "result": {},
                "finished_at": None,
            },
        )
        try:
            data = assemble_store_cart(run, store)
        except CartAgentError as error:
            diagnostic = _text(str(error), 500)
            attempt.status = CartAttempt.Status.FAILED
            attempt.summary = diagnostic or "Сервис не завершил одноэтапную сборку."
            attempt.result = {
                "mutation_unknown": error.mutation_possible,
                "error": diagnostic,
            }
            attempt.finished_at = timezone.now()
            attempt.save(update_fields=["status", "summary", "result", "finished_at"])
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
        _save_result(attempt, data)
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
        if attempt_needs_cleanup(attempt) and missing_count >= cleanup_threshold:
            try:
                cleanup_status = _cleanup_attempt(run, attempt)
            except CartAgentError:
                _record_outstanding_cleanup(
                    run,
                    attempt,
                    "Не удалось очистить неполную корзину.",
                )
                raise
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
        run = (
            CartRun.objects.select_for_update(skip_locked=True)
            .filter(status=CartRun.Status.CLEANUP_PENDING)
            .order_by("cleanup_requested_at", "created_at")
            .first()
        )
        if not run:
            return None
        if browser_login_session_blocks_worker():
            return None
        if CartRun.objects.filter(
            status__in=[CartRun.Status.PROCESSING, CartRun.Status.CLEANING]
        ).exclude(pk=run.pk).exists():
            return None
        run.status = CartRun.Status.CLEANING
        run.started_at = timezone.now()
        run.error = ""
        run.save(update_fields=["status", "started_at", "error"])
        return run


def process_cart_cleanup(run: CartRun) -> None:
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
        run = (
            CartRun.objects.select_for_update(skip_locked=True)
            .filter(status=CartRun.Status.PENDING)
            .order_by("created_at")
            .first()
        )
        if not run:
            return None
        # A human and the agent must never drive the shared Camofox process at
        # the same time. Even expired rows block until remote close is confirmed.
        if browser_login_session_blocks_worker():
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
        run.finished_at = None
        run.error = ""
        run.save(update_fields=["status", "started_at", "finished_at", "error"])
        return run

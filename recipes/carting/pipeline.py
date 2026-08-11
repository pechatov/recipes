from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from recipes.models import CartAttempt, CartItemMatch, CartRun

from .client import CartAgentError, assemble_store_cart, inspect_store_cart


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


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _save_result(attempt: CartAttempt, data: dict) -> None:
    raw_status = _text(data.get("status"), 24)
    status = raw_status if raw_status in AGENT_STATUSES else CartAttempt.Status.FAILED
    expected = [item["name"] for item in attempt.run.ingredient_snapshot]
    by_name = {}
    for item in data.get("items", []):
        if isinstance(item, dict):
            by_name.setdefault(_text(item.get("ingredient_name"), 180).casefold(), item)

    attempt.matches.all().delete()
    matches = []
    for order, ingredient_name in enumerate(expected):
        item = by_name.get(ingredient_name.casefold(), {})
        quality = _text(item.get("quality"), 16)
        if quality not in MATCH_QUALITIES:
            quality = CartItemMatch.MatchQuality.MISSING
        try:
            package_count = max(0, min(int(item.get("package_count") or 0), 100))
        except (TypeError, ValueError):
            package_count = 0
        matches.append(
            CartItemMatch(
                attempt=attempt,
                ingredient_name=ingredient_name,
                requested_quantity=_text(item.get("requested_quantity"), 80),
                product_name=_text(item.get("product_name"), 300),
                product_url=_text(item.get("product_url"), 2048),
                package_count=package_count,
                quality=quality,
                warning=_text(item.get("warning"), 500),
                order=order,
            )
        )
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

    attempt.status = status
    attempt.cart_url = _text(data.get("cart_url"), 2048)
    attempt.summary = _text(data.get("summary"), 500)
    attempt.result = data
    attempt.finished_at = timezone.now()
    attempt.save(
        update_fields=["status", "cart_url", "summary", "result", "finished_at"]
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
    assembled_stores = set()
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
            data = inspect_store_cart(run, store)
        except CartAgentError:
            attempt.status = CartAttempt.Status.FAILED
            attempt.summary = "Браузерный агент не завершил попытку."
            attempt.finished_at = timezone.now()
            attempt.save(update_fields=["status", "summary", "finished_at"])
            raise
        _save_result(attempt, data)
        run.next_store_index += 1
        run.save(update_fields=["next_store_index"])

        if attempt.status == CartAttempt.Status.EXACT:
            try:
                _save_result(attempt, assemble_store_cart(run, store))
                assembled_stores.add(store)
            except CartAgentError:
                attempt.status = CartAttempt.Status.FAILED
                attempt.summary = "Браузерный агент не завершил добавление товаров."
                attempt.finished_at = timezone.now()
                attempt.save(update_fields=["status", "summary", "finished_at"])
                raise
            if attempt.status == CartAttempt.Status.EXACT:
                run.status = CartRun.Status.COMPLETED
                run.selected_attempt = attempt
                run.finished_at = timezone.now()
                run.error = ""
                run.save(update_fields=["status", "selected_attempt", "finished_at", "error"])
                return
        if attempt.status == CartAttempt.Status.LOGIN_REQUIRED:
            run.status = CartRun.Status.LOGIN_REQUIRED
            run.next_store_index -= 1
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "next_store_index", "finished_at"])
            return
        if attempt.status == CartAttempt.Status.BLOCKED:
            # CAPTCHA is recoverable by the account owner in the persistent
            # browser profile. Pause on this exact store so Retry continues
            # here after the human verification instead of skipping vendors.
            run.status = CartRun.Status.LOGIN_REQUIRED
            run.next_store_index -= 1
            run.finished_at = timezone.now()
            run.error = "Сайт попросил ручное подтверждение в браузере."
            run.save(
                update_fields=["status", "next_store_index", "finished_at", "error"]
            )
            return

    best = _best_attempt(run)
    if best and best.store not in assembled_stores:
        try:
            _save_result(best, assemble_store_cart(run, best.store))
        except CartAgentError:
            best.status = CartAttempt.Status.FAILED
            best.summary = "Браузерный агент не завершил добавление товаров."
            best.finished_at = timezone.now()
            best.save(update_fields=["status", "summary", "finished_at"])
            raise
    run.selected_attempt = best
    run.finished_at = timezone.now()
    if best and best.status == CartAttempt.Status.EXACT:
        run.status = CartRun.Status.COMPLETED
        run.error = ""
    elif best and best.status == CartAttempt.Status.LOGIN_REQUIRED:
        run.status = CartRun.Status.LOGIN_REQUIRED
        run.error = ""
    elif best and best.status not in {CartAttempt.Status.BLOCKED, CartAttempt.Status.FAILED}:
        run.status = CartRun.Status.REVIEW
        run.error = ""
    else:
        run.status = CartRun.Status.FAILED
        run.error = "Ни один магазин не удалось проверить автоматически."
    run.save(update_fields=["status", "selected_attempt", "finished_at", "error"])


def claim_cart_run():
    with transaction.atomic():
        run = (
            CartRun.objects.select_for_update(skip_locked=True)
            .filter(status=CartRun.Status.PENDING)
            .order_by("created_at")
            .first()
        )
        if not run:
            return None
        # One persistent browser profile must not be shared by concurrent jobs.
        if CartRun.objects.filter(status=CartRun.Status.PROCESSING).exclude(pk=run.pk).exists():
            return None
        run.status = CartRun.Status.PROCESSING
        run.started_at = timezone.now()
        run.finished_at = None
        run.error = ""
        run.save(update_fields=["status", "started_at", "finished_at", "error"])
        return run

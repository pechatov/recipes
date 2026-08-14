from django.db.models import Q
from django.utils import timezone

from recipes.models import BrowserLoginSession, CartAttempt, CartRun

from .browser_login import BrowserLoginError, stop_session


BROWSER_BLOCKING_STATUSES = [
    BrowserLoginSession.Status.STARTING,
    BrowserLoginSession.Status.ACTIVE,
    BrowserLoginSession.Status.STOPPING,
    BrowserLoginSession.Status.COMPLETING,
]


def resume_cart_run_after_login(run: CartRun) -> None:
    if run.status not in {CartRun.Status.LOGIN_REQUIRED, CartRun.Status.FAILED}:
        return
    if run.cleanup_requested_at and not run.cleaned_at:
        run.status = CartRun.Status.CLEANUP_PENDING
        fields = ["status", "finished_at", "error"]
    else:
        if run.status == CartRun.Status.FAILED:
            retry_stores = {
                attempt.store
                for attempt in run.attempts.only("store", "status", "result")
                if attempt.status == CartAttempt.Status.BLOCKED
                or (
                    isinstance(attempt.result, dict)
                    and attempt.result.get("reason") == "store_unavailable"
                )
            }
            for index, store in enumerate(run.store_priority):
                if store in retry_stores:
                    run.next_store_index = index
                    break
        run.status = CartRun.Status.PENDING
        fields = ["status", "next_store_index", "finished_at", "error"]
    run.finished_at = None
    run.error = ""
    run.save(update_fields=fields)


def reconcile_expired_browser_login_sessions() -> bool:
    """Close expired controller sessions before releasing their database lock.

    The caller must hold CART_BROWSER_LOCK in a transaction. Returning False
    means at least one remote close was not confirmed and workers must wait.
    """
    now = timezone.now()
    sessions = list(
        BrowserLoginSession.objects.select_for_update().filter(
            Q(
                status__in=[
                    BrowserLoginSession.Status.STOPPING,
                    BrowserLoginSession.Status.COMPLETING,
                ]
            )
            | Q(
                status__in=[
                    BrowserLoginSession.Status.STARTING,
                    BrowserLoginSession.Status.ACTIVE,
                ],
                expires_at__lte=now,
            )
        )
    )
    for login_session in sessions:
        try:
            stop_session(login_session.remote_session_id)
        except BrowserLoginError:
            return False
        previous_status = login_session.status
        if previous_status == BrowserLoginSession.Status.COMPLETING:
            login_session.status = BrowserLoginSession.Status.COMPLETED
            login_session.error = ""
        elif previous_status == BrowserLoginSession.Status.STOPPING:
            login_session.status = BrowserLoginSession.Status.FAILED
            login_session.error = "Окно входа закрыто при остановке сборки."
        else:
            login_session.status = BrowserLoginSession.Status.EXPIRED
        login_session.finished_at = now
        login_session.save(update_fields=["status", "finished_at", "error"])
        if (
            previous_status == BrowserLoginSession.Status.COMPLETING
            and login_session.run_id
        ):
            run = CartRun.objects.select_for_update().get(pk=login_session.run_id)
            resume_cart_run_after_login(run)
    return True


def reconcile_missing_browser_login_session(session_pk: int) -> bool:
    """Release a DB session only after its absence is reconfirmed remotely.

    The caller must hold CART_BROWSER_LOCK in a transaction. A failed DELETE
    leaves the blocking row unchanged, preserving the worker/browser boundary.
    """
    login_session = BrowserLoginSession.objects.select_for_update().get(pk=session_pk)
    if login_session.status not in BROWSER_BLOCKING_STATUSES:
        return True
    try:
        stop_session(login_session.remote_session_id)
    except BrowserLoginError:
        return False
    login_session.status = BrowserLoginSession.Status.FAILED
    login_session.finished_at = timezone.now()
    login_session.error = "Controller подтвердил отсутствие удалённой сессии."
    login_session.save(update_fields=["status", "finished_at", "error"])
    return True


def browser_login_session_blocks_worker() -> bool:
    return BrowserLoginSession.objects.filter(
        status__in=BROWSER_BLOCKING_STATUSES,
    ).exists()

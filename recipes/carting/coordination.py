from django.utils import timezone

from recipes.models import BrowserLoginSession

from .browser_login import BrowserLoginError, stop_session


BROWSER_BLOCKING_STATUSES = [
    BrowserLoginSession.Status.STARTING,
    BrowserLoginSession.Status.ACTIVE,
]


def reconcile_expired_browser_login_sessions() -> bool:
    """Close expired controller sessions before releasing their database lock.

    The caller must hold CART_BROWSER_LOCK in a transaction. Returning False
    means at least one remote close was not confirmed and workers must wait.
    """
    now = timezone.now()
    sessions = list(
        BrowserLoginSession.objects.select_for_update().filter(
            status__in=BROWSER_BLOCKING_STATUSES,
            expires_at__lte=now,
        )
    )
    for login_session in sessions:
        try:
            stop_session(login_session.remote_session_id)
        except BrowserLoginError:
            return False
        login_session.status = BrowserLoginSession.Status.EXPIRED
        login_session.finished_at = now
        login_session.save(update_fields=["status", "finished_at"])
    return True


def browser_login_session_blocks_worker() -> bool:
    return BrowserLoginSession.objects.filter(
        status__in=BROWSER_BLOCKING_STATUSES,
    ).exists()

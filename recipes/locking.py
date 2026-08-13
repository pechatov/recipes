from django.db import transaction

from .models import ApplicationLock


CART_BROWSER_LOCK = "cart_browser"
REGISTRATION_LOCK = "registration"


def acquire_application_lock(name: str) -> ApplicationLock:
    """Lock a seeded singleton row for the rest of the current transaction."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Application locks require transaction.atomic()")
    return ApplicationLock.objects.select_for_update().get(pk=name)

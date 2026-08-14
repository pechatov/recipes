import logging
import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from recipes.carting.client import CartAgentError
from recipes.carting.pipeline import (
    claim_cart_run,
    claim_cleanup_run,
    expire_unconfirmed_cart_runs,
    finish_requested_cart_stop,
    process_cart_cleanup,
    process_cart_run,
    record_unexpected_browser_failure,
)
from recipes.models import CartRun


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process queued supermarket cart assembly jobs"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=3.0)

    @staticmethod
    def recover_stale_jobs():
        now = timezone.now()
        cutoff = now - timedelta(minutes=30)
        operation_cutoff = now - timedelta(
            seconds=max(
                settings.CART_AI_TIMEOUT_SECONDS,
                settings.CART_ADAPTER_TIMEOUT_SECONDS,
            )
            + 60
        )
        uncertain_mutations = CartRun.objects.filter(
            status__in=[CartRun.Status.PROCESSING, CartRun.Status.CLEANING],
            browser_operation_started_at__lte=operation_cutoff,
        ).update(
            status=CartRun.Status.MANUAL_CHECK,
            started_at=None,
            finished_at=now,
            error=(
                "Worker был прерван во время операции с Яндекс Едой. "
                "Проверьте корзину вручную перед следующим запуском."
            ),
        )
        orphaned_terminal_reservations = CartRun.objects.filter(
            status=CartRun.Status.FAILED,
            browser_operation_started_at__isnull=False,
        ).update(
            status=CartRun.Status.MANUAL_CHECK,
            finished_at=now,
            error=(
                "Worker завершился с ошибкой во время операции с Яндекс Едой. "
                "Проверьте корзину вручную перед следующим запуском."
            ),
        )
        recovered_runs = CartRun.objects.filter(
            status=CartRun.Status.PROCESSING,
            started_at__lt=cutoff,
            browser_operation_started_at__isnull=True,
        ).update(
            status=CartRun.Status.PENDING,
            started_at=None,
            error="",
        )
        recovered_cleanups = CartRun.objects.filter(
            status=CartRun.Status.CLEANING,
            started_at__lt=cutoff,
            browser_operation_started_at__isnull=True,
        ).update(
            status=CartRun.Status.CLEANUP_PENDING,
            started_at=None,
            error="",
        )
        return (
            uncertain_mutations
            + orphaned_terminal_reservations
            + recovered_runs
            + recovered_cleanups
        )

    def handle(self, *args, **options):
        recovered = self.recover_stale_jobs()
        if recovered:
            self.stdout.write(f"Recovered {recovered} stale cart run(s)")
        while True:
            expired = expire_unconfirmed_cart_runs()
            if expired:
                self.stdout.write(f"Queued cleanup for {expired} expired cart(s)")
            run = claim_cleanup_run()
            cleaning = bool(run)
            if not run:
                run = claim_cart_run()
            if run:
                try:
                    if cleaning:
                        process_cart_cleanup(run)
                    else:
                        process_cart_run(run)
                    finish_requested_cart_stop(run)
                except CartAgentError as error:
                    if finish_requested_cart_stop(
                        run,
                        mutation_unknown=error.mutation_possible,
                    ):
                        action = "cleanup" if cleaning else "assembly"
                        self.stdout.write(f"Cart {action} {run.pk} stopped")
                    elif error.mutation_possible:
                        run.status = CartRun.Status.MANUAL_CHECK
                        run.error = (
                            "Результат изменения корзины неизвестен. Откройте "
                            "Яндекс Еду, проверьте корзину и подтвердите ручную проверку."
                        )
                    else:
                        run.status = CartRun.Status.FAILED
                        run.error = str(error)[:2000]
                    run.finished_at = timezone.now()
                    run.save(update_fields=["status", "error", "finished_at"])
                    stopped_after_error = finish_requested_cart_stop(
                        run,
                        mutation_unknown=error.mutation_possible,
                    )
                    if not stopped_after_error:
                        action = "cleanup" if cleaning else "assembly"
                        self.stderr.write(f"Cart {action} {run.pk} failed: {error}")
                except Exception as error:
                    if not finish_requested_cart_stop(run, mutation_unknown=True):
                        recovered_browser_failure = record_unexpected_browser_failure(
                            run,
                            error=error,
                        )
                    else:
                        recovered_browser_failure = True
                    if not recovered_browser_failure:
                        run.status = CartRun.Status.FAILED
                        run.error = "Внутренняя ошибка сборки. Подробности сохранены в журнале."
                        run.finished_at = timezone.now()
                        run.save(update_fields=["status", "error", "finished_at"])
                        finish_requested_cart_stop(
                            run,
                            mutation_unknown=True,
                        )
                    logger.exception("Cart run %s failed unexpectedly", run.pk)
            if options["once"]:
                return
            if not run:
                time.sleep(max(0.2, options["poll_seconds"]))

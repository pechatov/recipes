import logging
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from recipes.carting.client import CartAgentError
from recipes.carting.pipeline import claim_cart_run, process_cart_run
from recipes.models import CartRun


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process queued supermarket cart assembly jobs"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=3.0)

    @staticmethod
    def recover_stale_jobs():
        cutoff = timezone.now() - timedelta(minutes=30)
        return CartRun.objects.filter(
            status=CartRun.Status.PROCESSING,
            started_at__lt=cutoff,
        ).update(
            status=CartRun.Status.PENDING,
            started_at=None,
            error="",
        )

    def handle(self, *args, **options):
        recovered = self.recover_stale_jobs()
        if recovered:
            self.stdout.write(f"Recovered {recovered} stale cart run(s)")
        while True:
            run = claim_cart_run()
            if run:
                try:
                    process_cart_run(run)
                except CartAgentError as error:
                    run.status = CartRun.Status.FAILED
                    run.error = str(error)[:2000]
                    run.finished_at = timezone.now()
                    run.save(update_fields=["status", "error", "finished_at"])
                    self.stderr.write(f"Cart run {run.pk} failed: {error}")
                except Exception:
                    run.status = CartRun.Status.FAILED
                    run.error = "Внутренняя ошибка сборки. Подробности сохранены в журнале."
                    run.finished_at = timezone.now()
                    run.save(update_fields=["status", "error", "finished_at"])
                    logger.exception("Cart run %s failed unexpectedly", run.pk)
            if options["once"]:
                return
            if not run:
                time.sleep(max(0.2, options["poll_seconds"]))

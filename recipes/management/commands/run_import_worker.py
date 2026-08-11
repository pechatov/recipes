import time
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from recipes.importing.exceptions import ImportPipelineError
from recipes.importing.pipeline import process_import_job
from recipes.models import ImportJob


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process queued recipe imports"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one job and exit")
        parser.add_argument("--poll-seconds", type=float, default=3.0)

    @staticmethod
    def recover_stale_jobs():
        cutoff = timezone.now() - timedelta(minutes=15)
        return ImportJob.objects.filter(
            status=ImportJob.Status.PROCESSING,
            started_at__lt=cutoff,
            recipe__isnull=True,
        ).update(
            status=ImportJob.Status.PENDING,
            started_at=None,
            error="",
        )

    @staticmethod
    def claim_job():
        with transaction.atomic():
            job = (
                ImportJob.objects.select_for_update(skip_locked=True)
                .filter(status=ImportJob.Status.PENDING)
                .order_by("created_at")
                .first()
            )
            if not job:
                return None
            job.status = ImportJob.Status.PROCESSING
            job.started_at = timezone.now()
            job.finished_at = None
            job.error = ""
            job.attempts += 1
            job.save(
                update_fields=["status", "started_at", "finished_at", "error", "attempts"]
            )
            return job

    def handle(self, *args, **options):
        recovered = self.recover_stale_jobs()
        if recovered:
            self.stdout.write(f"Recovered {recovered} stale import job(s)")
        while True:
            job = self.claim_job()
            if job:
                try:
                    recipes = process_import_job(job)
                except ImportPipelineError as error:
                    job.status = ImportJob.Status.FAILED
                    job.error = str(error)[:2000]
                    job.finished_at = timezone.now()
                    job.save(update_fields=["status", "error", "finished_at"])
                    self.stderr.write(f"Import {job.pk} failed: {error}")
                except Exception:
                    job.status = ImportJob.Status.FAILED
                    job.error = (
                        "Внутренняя ошибка импорта. "
                        "Подробности сохранены в журнале сервера."
                    )
                    job.finished_at = timezone.now()
                    job.save(update_fields=["status", "error", "finished_at"])
                    logger.exception("Import %s failed unexpectedly", job.pk)
                    self.stderr.write(f"Import {job.pk} failed unexpectedly")
                else:
                    self.stdout.write(
                        f"Import {job.pk} completed: {len(recipes)} recipe(s) created"
                    )
            if options["once"]:
                return
            if not job:
                time.sleep(max(0.2, options["poll_seconds"]))

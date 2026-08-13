import time
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from recipes.importing.exceptions import ImportPipelineError
from recipes.importing.pipeline import process_import_job, process_recipe_refinement
from recipes.models import ImportJob, RecipeRefinement


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process queued recipe imports"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one job and exit")
        parser.add_argument("--poll-seconds", type=float, default=3.0)

    @staticmethod
    def recover_stale_jobs():
        cutoff = timezone.now() - timedelta(minutes=15)
        imports = ImportJob.objects.filter(
            status=ImportJob.Status.PROCESSING,
            started_at__lt=cutoff,
            recipe__isnull=True,
        ).update(
            status=ImportJob.Status.PENDING,
            started_at=None,
            error="",
        )
        refinements = RecipeRefinement.objects.filter(
            status=RecipeRefinement.Status.PROCESSING,
            started_at__lt=cutoff,
        ).update(
            status=RecipeRefinement.Status.PENDING,
            started_at=None,
            error="",
        )
        return imports + refinements

    @staticmethod
    def claim_task():
        with transaction.atomic():
            import_job = (
                ImportJob.objects.select_for_update(skip_locked=True)
                .filter(status=ImportJob.Status.PENDING)
                .order_by("created_at")
                .first()
            )
            refinement = (
                RecipeRefinement.objects.select_for_update(skip_locked=True)
                .filter(status=RecipeRefinement.Status.PENDING)
                .order_by("created_at")
                .first()
            )
            candidates = [item for item in (import_job, refinement) if item]
            if not candidates:
                return None
            task = min(candidates, key=lambda item: item.created_at)
            task.status = task.__class__.Status.PROCESSING
            task.started_at = timezone.now()
            task.finished_at = None
            task.error = ""
            task.attempts += 1
            task.save(
                update_fields=["status", "started_at", "finished_at", "error", "attempts"]
            )
            return task

    def handle(self, *args, **options):
        recovered = self.recover_stale_jobs()
        if recovered:
            self.stdout.write(f"Recovered {recovered} stale import job(s)")
        while True:
            task = self.claim_task()
            if task:
                try:
                    if isinstance(task, RecipeRefinement):
                        recipe = process_recipe_refinement(task)
                        result_message = f"Refinement {task.pk} completed: {recipe.pk}"
                    else:
                        recipes = process_import_job(task)
                        result_message = (
                            f"Import {task.pk} completed: {len(recipes)} recipe(s) created"
                        )
                except ImportPipelineError as error:
                    task.status = task.__class__.Status.FAILED
                    task.error = str(error)[:2000]
                    task.finished_at = timezone.now()
                    task.save(update_fields=["status", "error", "finished_at"])
                    self.stderr.write(f"Task {task.pk} failed: {error}")
                except Exception:
                    task.status = task.__class__.Status.FAILED
                    task.error = (
                        "Внутренняя ошибка обработки рецепта. "
                        "Подробности сохранены в журнале сервера."
                    )
                    task.finished_at = timezone.now()
                    task.save(update_fields=["status", "error", "finished_at"])
                    logger.exception("Recipe task %s failed unexpectedly", task.pk)
                    self.stderr.write(f"Task {task.pk} failed unexpectedly")
                else:
                    self.stdout.write(result_message)
            if options["once"]:
                return
            if not task:
                time.sleep(max(0.2, options["poll_seconds"]))

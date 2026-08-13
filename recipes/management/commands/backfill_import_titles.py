from django.core.management.base import BaseCommand

from recipes.importing.extractors import fetch_source_title, youtube_video_id
from recipes.models import ImportJob


class Command(BaseCommand):
    help = "Replace technical YouTube import titles with video titles"

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true")

    def handle(self, *args, **options):
        jobs = ImportJob.objects.filter(source_type=ImportJob.SourceType.YOUTUBE)
        probe_job = jobs.order_by("-created_at").first()
        technical_jobs = [
            job
            for job in jobs.iterator()
            if job.source_title in {"", f"YouTube {youtube_video_id(job.source_url) or ''}"}
        ]
        if options["check"]:
            if not probe_job:
                self.stdout.write("No YouTube imports available for title lookup check")
                return
            sample_id = youtube_video_id(probe_job.source_url)
            if sample_id and fetch_source_title(probe_job.source_url):
                self.stdout.write("YouTube title lookup is healthy")
                return
            raise RuntimeError("YouTube title lookup is unavailable")

        updated = 0
        for job in technical_jobs:
            title = fetch_source_title(job.source_url)
            if title:
                job.source_title = title
                job.save(update_fields=["source_title"])
                updated += 1
        self.stdout.write(f"Updated {updated} YouTube import title(s)")

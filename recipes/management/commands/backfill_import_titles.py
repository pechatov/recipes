from django.core.management.base import BaseCommand
from django.db.models import F
from django.utils import timezone

from recipes.importing.extractors import (
    YOUTUBE_OEMBED_PROBE_VIDEO_ID,
    fetch_source_title,
    youtube_video_id,
)
from recipes.models import ImportJob


class Command(BaseCommand):
    help = "Replace technical YouTube import titles with video titles"

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1 or limit > 100:
            raise ValueError("--limit must be between 1 and 100")
        if options["check"]:
            probe_url = (
                "https://www.youtube.com/watch?v="
                f"{YOUTUBE_OEMBED_PROBE_VIDEO_ID}"
            )
            if fetch_source_title(probe_url):
                self.stdout.write("YouTube title lookup is healthy")
                return
            raise RuntimeError("YouTube title lookup is unavailable")

        jobs = ImportJob.objects.filter(
            source_type=ImportJob.SourceType.YOUTUBE
        ).order_by(
            F("source_title_checked_at").asc(nulls_first=True),
            "created_at",
        )
        technical_jobs = []
        for job in jobs.iterator():
            technical_title = f"YouTube {youtube_video_id(job.source_url) or ''}"
            if job.source_title in {"", technical_title}:
                technical_jobs.append(job)
                if len(technical_jobs) >= limit:
                    break

        updated = 0
        for job in technical_jobs:
            job.source_title_checked_at = timezone.now()
            job.save(update_fields=["source_title_checked_at"])
            title = fetch_source_title(job.source_url)
            if title:
                job.source_title = title
                job.save(update_fields=["source_title"])
                updated += 1
        self.stdout.write(f"Updated {updated} YouTube import title(s)")

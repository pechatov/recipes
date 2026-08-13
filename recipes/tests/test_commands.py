from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from recipes.models import ImportJob


class BackfillImportTitlesTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("import-title-owner")

    @patch(
        "recipes.management.commands.backfill_import_titles.fetch_source_title",
        return_value="Chicken Parmi at Home",
    )
    def test_replaces_technical_youtube_title(self, fetch_title):
        job = ImportJob.objects.create(
            source_url="https://www.youtube.com/watch?v=kHf5Jdwwxns",
            source_title="YouTube kHf5Jdwwxns",
            source_type=ImportJob.SourceType.YOUTUBE,
            requested_by=self.user,
        )

        call_command("backfill_import_titles")

        job.refresh_from_db()
        self.assertEqual(job.source_title, "Chicken Parmi at Home")
        fetch_title.assert_called_once_with(job.source_url)

    @patch(
        "recipes.management.commands.backfill_import_titles.fetch_source_title",
        return_value="Chicken Parmi at Home",
    )
    def test_check_uses_stable_probe_instead_of_user_import(self, fetch_title):
        ImportJob.objects.create(
            source_url="https://www.youtube.com/watch?v=kHf5Jdwwxns",
            source_title="Chicken Parmi at Home",
            source_type=ImportJob.SourceType.YOUTUBE,
            requested_by=self.user,
        )

        call_command("backfill_import_titles", check=True)

        fetch_title.assert_called_once_with(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    @patch(
        "recipes.management.commands.backfill_import_titles.fetch_source_title",
        return_value="Stable probe title",
    )
    def test_check_does_not_require_existing_imports(self, fetch_title):
        call_command("backfill_import_titles", check=True)

        fetch_title.assert_called_once_with(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    @patch(
        "recipes.management.commands.backfill_import_titles.fetch_source_title",
        return_value="",
    )
    def test_check_fails_when_title_lookup_is_unavailable(self, fetch_title):
        job = ImportJob.objects.create(
            source_url="https://www.youtube.com/watch?v=kHf5Jdwwxns",
            source_title="YouTube kHf5Jdwwxns",
            source_type=ImportJob.SourceType.YOUTUBE,
            requested_by=self.user,
        )

        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            call_command("backfill_import_titles", check=True)

        fetch_title.assert_called_once_with(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    @patch(
        "recipes.management.commands.backfill_import_titles.fetch_source_title",
        side_effect=lambda url: f"Title for {url.rsplit('=', 1)[-1]}",
    )
    def test_backfill_respects_limit_and_processes_newest_first(self, fetch_title):
        jobs = [
            ImportJob.objects.create(
                source_url=f"https://www.youtube.com/watch?v=video00000{index}",
                source_title=f"YouTube video00000{index}",
                source_type=ImportJob.SourceType.YOUTUBE,
                requested_by=self.user,
            )
            for index in range(3)
        ]

        call_command("backfill_import_titles", limit=2)

        for job in jobs:
            job.refresh_from_db()
        self.assertEqual(jobs[0].source_title, "YouTube video000000")
        self.assertEqual(jobs[1].source_title, "Title for video000001")
        self.assertEqual(jobs[2].source_title, "Title for video000002")

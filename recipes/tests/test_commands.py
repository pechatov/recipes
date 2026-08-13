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
    def test_check_probes_latest_youtube_import(self, fetch_title):
        job = ImportJob.objects.create(
            source_url="https://www.youtube.com/watch?v=kHf5Jdwwxns",
            source_title="Chicken Parmi at Home",
            source_type=ImportJob.SourceType.YOUTUBE,
            requested_by=self.user,
        )

        call_command("backfill_import_titles", check=True)

        fetch_title.assert_called_once_with(job.source_url)

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

        fetch_title.assert_called_once_with(job.source_url)

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class StoreSelectionMigrationTests(TransactionTestCase):
    migrate_from = ("recipes", "0014_merge_reciperefinement")
    migrate_to = ("recipes", "0015_alter_storepreference_options_and_more")

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])
        old_apps = self.executor.loader.project_state([self.migrate_from]).apps
        User = old_apps.get_model("auth", "User")
        StorePreference = old_apps.get_model("recipes", "StorePreference")
        user = User.objects.create(username="migration-user")
        StorePreference.objects.create(
            user_id=user.pk,
            store="auchan",
            position=0,
            enabled=False,
        )
        StorePreference.objects.create(
            user_id=user.pk,
            store="perekrestok",
            position=1,
            enabled=True,
        )
        StorePreference.objects.create(
            user_id=user.pk,
            store="lavka",
            position=2,
            enabled=True,
        )
        self.user_id = user.pk

    def tearDown(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_forward_selection_and_rollback_restore_exact_legacy_flags(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_to])
        new_apps = self.executor.loader.project_state([self.migrate_to]).apps
        StorePreference = new_apps.get_model("recipes", "StorePreference")
        self.assertEqual(
            list(
                StorePreference.objects.filter(
                    user_id=self.user_id,
                    enabled=True,
                ).values_list("store", flat=True)
            ),
            ["perekrestok"],
        )

        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])
        old_apps = self.executor.loader.project_state([self.migrate_from]).apps
        StorePreference = old_apps.get_model("recipes", "StorePreference")
        self.assertEqual(
            dict(
                StorePreference.objects.filter(user_id=self.user_id).values_list(
                    "store", "enabled"
                )
            ),
            {
                "auchan": False,
                "perekrestok": True,
                "lavka": True,
            },
        )

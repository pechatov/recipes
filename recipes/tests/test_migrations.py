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


class RecipeSourceLinksMigrationTests(TransactionTestCase):
    migrate_from = ("recipes", "0019_browserloginsession_transition_started_at")
    migrate_to = ("recipes", "0020_recipe_source_links")

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_sources_are_copied_without_losing_provenance(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        Recipe = executor.loader.project_state([self.migrate_from]).apps.get_model(
            "recipes", "Recipe"
        )
        sources = [
            ("https://example.com/recipe.txt", "text_source_url"),
            ("https://youtu.be/dQw4w9WgXcQ", "video_url"),
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "video_url"),
            ("https://m.youtube.com/shorts/dQw4w9WgXcQ", "video_url"),
            ("https://youtube.com.example.org/recipe", "text_source_url"),
            ("", "text_source_url"),
        ]
        for index, (url, _) in enumerate(sources):
            Recipe.objects.create(title=f"Recipe {index}", slug=f"recipe-{index}", source_url=url)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        Recipe = executor.loader.project_state([self.migrate_to]).apps.get_model(
            "recipes", "Recipe"
        )
        for index, (url, field) in enumerate(sources):
            recipe = Recipe.objects.get(slug=f"recipe-{index}")
            self.assertEqual(recipe.source_url, url)
            self.assertEqual(getattr(recipe, field), url)
            other = "video_url" if field == "text_source_url" else "text_source_url"
            self.assertEqual(getattr(recipe, other), "")

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        Recipe = executor.loader.project_state([self.migrate_from]).apps.get_model(
            "recipes", "Recipe"
        )
        self.assertCountEqual(Recipe.objects.values_list("source_url", flat=True), [url for url, _ in sources])

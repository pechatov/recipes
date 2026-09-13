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


class MainProteinMigrationTests(TransactionTestCase):
    migrate_from = ("recipes", "0021_recipenutrition")
    migrate_to = ("recipes", "0022_recipe_main_protein")

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])
        old_apps = self.executor.loader.project_state([self.migrate_from]).apps
        Category = old_apps.get_model("recipes", "Category")
        Recipe = old_apps.get_model("recipes", "Recipe")
        RecipeIngredient = old_apps.get_model("recipes", "RecipeIngredient")
        main_course, _ = Category.objects.get_or_create(
            slug="main-course", defaults={"name": "Второе блюдо"}
        )
        soup, _ = Category.objects.get_or_create(slug="soup", defaults={"name": "Суп"})
        pie = Recipe.objects.create(title="Коттедж-пай", slug="cottage-pie")
        pie.categories.add(main_course)
        RecipeIngredient.objects.create(recipe=pie, name="Говяжий бульон", order=0)
        RecipeIngredient.objects.create(recipe=pie, name="Говяжий фарш", order=1)
        chicken_soup = Recipe.objects.create(title="Куриный суп", slug="chicken-soup")
        chicken_soup.categories.add(soup)
        RecipeIngredient.objects.create(recipe=chicken_soup, name="Курица", order=0)
        veggie = Recipe.objects.create(title="Рагу", slug="ragu")
        veggie.categories.add(main_course)
        RecipeIngredient.objects.create(recipe=veggie, name="Кабачок", order=0)
        self.ids = (pie.pk, chicken_soup.pk, veggie.pk)

    def tearDown(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_forward_fills_badges_only_for_main_courses_with_known_protein(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_to])
        new_apps = self.executor.loader.project_state([self.migrate_to]).apps
        Recipe = new_apps.get_model("recipes", "Recipe")

        pie, chicken_soup, veggie = (Recipe.objects.get(pk=pk) for pk in self.ids)

        self.assertEqual(pie.main_protein, "beef")
        self.assertEqual(chicken_soup.main_protein, "")
        self.assertEqual(veggie.main_protein, "")


class RecipeNutritionMigrationTests(TransactionTestCase):
    migrate_from = ("recipes", "0020_recipe_source_links")
    migrate_to = ("recipes", "0021_recipenutrition")

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])
        old_apps = self.executor.loader.project_state([self.migrate_from]).apps
        Recipe = old_apps.get_model("recipes", "Recipe")
        RecipeIngredient = old_apps.get_model("recipes", "RecipeIngredient")
        estimated = Recipe.objects.create(title="Картошка", slug="kartoshka", servings=4)
        RecipeIngredient.objects.create(
            recipe=estimated, name="Картофель", quantity="500", unit="г", order=0
        )
        all_fields = [
            "calories_per_serving", "proteins_per_serving", "fats_per_serving",
            "carbohydrates_per_serving", "calories_per_100g", "proteins_per_100g",
            "fats_per_100g", "carbohydrates_per_100g",
        ]
        manual = Recipe.objects.create(
            title="Ручной", slug="ruchnoi", servings=2,
            calories_per_serving="100", proteins_per_serving="1", fats_per_serving="2",
            carbohydrates_per_serving="3", calories_per_100g="50", proteins_per_100g="0.5",
            fats_per_100g="1", carbohydrates_per_100g="1.5",
            nutrition_manual_fields=all_fields,
        )
        self.ids = (estimated.pk, manual.pk)

    def tearDown(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_forward_estimates_missing_values_and_keeps_manual_ones(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_to])
        new_apps = self.executor.loader.project_state([self.migrate_to]).apps
        RecipeNutrition = new_apps.get_model("recipes", "RecipeNutrition")

        estimated = RecipeNutrition.objects.get(recipe_id=self.ids[0])
        manual = RecipeNutrition.objects.get(recipe_id=self.ids[1])

        self.assertEqual(estimated.source, "estimated")
        self.assertEqual(str(estimated.calories_per_serving), "96.2")
        self.assertEqual(str(estimated.calories_per_100g), "77.0")
        self.assertTrue(estimated.notes)
        self.assertEqual(manual.source, "manual")
        self.assertEqual(str(manual.calories_per_serving), "100.0")
        self.assertEqual(manual.notes, "")

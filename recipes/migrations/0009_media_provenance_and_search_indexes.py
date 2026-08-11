from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations, models


def classify_imported_media(apps, schema_editor):
    Recipe = apps.get_model("recipes", "Recipe")
    RecipeStep = apps.get_model("recipes", "RecipeStep")
    Recipe.objects.filter(cover__contains="/import-").update(cover_imported=True)
    RecipeStep.objects.filter(image__contains="/import-").update(image_imported=True)


class Migration(migrations.Migration):
    dependencies = [("recipes", "0008_recipe_import_enhancements")]

    operations = [
        TrigramExtension(),
        migrations.AddField(
            model_name="recipe",
            name="cover_imported",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddField(
            model_name="recipestep",
            name="image_imported",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.RunPython(classify_imported_media, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="recipe",
            index=GinIndex(
                fields=["title"],
                name="recipe_title_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
        migrations.AddIndex(
            model_name="recipe",
            index=GinIndex(
                fields=["description"],
                name="recipe_desc_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
        migrations.AddIndex(
            model_name="recipeingredient",
            index=GinIndex(
                fields=["name"],
                name="ingredient_name_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
    ]

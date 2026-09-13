from django.db import migrations, models

from recipes.categories import infer_main_protein


def fill_main_protein(apps, schema_editor):
    Recipe = apps.get_model("recipes", "Recipe")
    recipes = Recipe.objects.using(schema_editor.connection.alias)
    for recipe in recipes.filter(categories__slug="main-course").distinct():
        names = (
            recipe.ingredients.filter(is_pantry=False)
            .order_by("order", "pk")
            .values_list("name", flat=True)
        )
        protein = infer_main_protein(names)
        if protein:
            recipes.filter(pk=recipe.pk).update(main_protein=protein)


class Migration(migrations.Migration):

    dependencies = [
        ("recipes", "0021_recipenutrition"),
    ]

    operations = [
        migrations.AddField(
            model_name="recipe",
            name="main_protein",
            field=models.CharField(
                blank=True,
                choices=[
                    ("chicken", "Курица"),
                    ("beef", "Говядина"),
                    ("pork", "Свинина"),
                    ("fish", "Рыба"),
                ],
                help_text="Бейдж для вторых блюд: курица, говядина, свинина или рыба.",
                max_length=16,
                verbose_name="из чего блюдо",
            ),
        ),
        migrations.RunPython(fill_main_protein, migrations.RunPython.noop),
    ]

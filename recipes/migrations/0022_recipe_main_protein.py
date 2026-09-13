import re

from django.db import migrations, models


# Frozen copy of recipes.categories at the time of this migration so the
# backfill never depends on later changes to the app code.
_MAIN_PROTEIN_RULES = (
    ("chicken", re.compile(r"\b(?:кур(?:иц|ин|оч)|цыпл)")),
    ("beef", re.compile(r"\b(?:говя|телят)")),
    ("pork", re.compile(r"\b(?:свин|бекон|ветчин|купат|сало\b|шпик|окорок|грудинк)")),
    (
        "fish",
        re.compile(
            r"\b(?:рыб|лосос|сёмг|семг|форел|треск|тун(?:ец|ц)|минта|скумбр|судак|окун"
            r"|горбуш|кет[аы]\b|палтус|дорад|сибас|хек\b|камбал|зубатк|щук|карп(?!ач)|сельд(?!ер)"
            r"|сардин|пикш|тилапи|пангас|нерк|кижуч|анчоус)"
        ),
    ),
)
_MAIN_PROTEIN_SKIP = re.compile(r"бульон|яйц|яиц|соус|приправ|специ")


def infer_main_protein(ingredient_names) -> str:
    """Guess the main protein of a dish from ingredient names in recipe order."""
    for name in ingredient_names:
        value = str(name or "").lower()
        if not value or _MAIN_PROTEIN_SKIP.search(value):
            continue
        for slug, pattern in _MAIN_PROTEIN_RULES:
            if pattern.search(value):
                return slug
    return ""


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

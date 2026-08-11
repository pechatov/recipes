import re
from decimal import Decimal

import django.core.validators
from django.db import migrations, models


def link_existing_import_recipes(apps, schema_editor):
    ImportJob = apps.get_model("recipes", "ImportJob")
    through = ImportJob.recipes.through
    links = [
        through(importjob_id=job.pk, recipe_id=job.recipe_id)
        for job in ImportJob.objects.exclude(recipe_id=None).iterator()
    ]
    through.objects.bulk_create(links, ignore_conflicts=True)


def classify_existing_pantry_ingredients(apps, schema_editor):
    RecipeIngredient = apps.get_model("recipes", "RecipeIngredient")
    pattern = re.compile(
        r"(?:^|\s)(?:"
        r"соль|перец|паприк|куркум|кориандр|карри|зир|кумин|орегано|базилик|"
        r"тимьян|розмарин|лавров|корица|гвоздик|мускат|ванил|шафран|"
        r"сахар|сода|разрыхлител|крахмал|желатин|дрожж|уксус|"
        r"масло растительн|масло оливков|соевый соус|горчиц|мед|"
        r"чеснок сушен|лук сушен|специ|приправ|seasoning|spice|salt|pepper"
        r")",
        re.IGNORECASE,
    )
    thresholds = {
        "г": Decimal("100"),
        "гр": Decimal("100"),
        "g": Decimal("100"),
        "мл": Decimal("100"),
        "ml": Decimal("100"),
        "кг": Decimal("0.1"),
        "kg": Decimal("0.1"),
        "л": Decimal("0.1"),
        "l": Decimal("0.1"),
        "ст.л.": Decimal("6"),
        "ст.л": Decimal("6"),
        "стл": Decimal("6"),
        "tbsp": Decimal("6"),
        "ч.л.": Decimal("20"),
        "ч.л": Decimal("20"),
        "чл": Decimal("20"),
        "tsp": Decimal("20"),
    }
    pantry_ids = []
    for ingredient in RecipeIngredient.objects.only(
        "pk", "name", "quantity", "unit"
    ).iterator():
        unit = (ingredient.unit or "").lower().replace(" ", "").replace("ё", "е")
        amount = ingredient.quantity
        threshold = thresholds.get(unit)
        if amount is not None and threshold is not None and amount > threshold:
            continue
        pantry_name = bool(pattern.search((ingredient.name or "").replace("ё", "е")))
        tiny_measure = (
            amount is not None
            and unit in {"щепотка", "щепотки", "ч.л.", "чл", "tsp"}
            and amount <= 3
        )
        if pantry_name or tiny_measure:
            pantry_ids.append(ingredient.pk)
    RecipeIngredient.objects.filter(pk__in=pantry_ids).update(is_pantry=True)


class Migration(migrations.Migration):
    dependencies = [("recipes", "0007_alter_cartrun_status")]

    operations = [
        migrations.AddField(
            model_name="recipe",
            name="calories_per_100g",
            field=models.DecimalField(
                blank=True,
                decimal_places=1,
                max_digits=8,
                null=True,
                validators=[django.core.validators.MinValueValidator(Decimal("0"))],
                verbose_name="ккал на 100 г",
            ),
        ),
        migrations.AddField(
            model_name="recipe",
            name="calories_per_serving",
            field=models.DecimalField(
                blank=True,
                decimal_places=1,
                max_digits=8,
                null=True,
                validators=[django.core.validators.MinValueValidator(Decimal("0"))],
                verbose_name="ккал на порцию",
            ),
        ),
        migrations.AddField(
            model_name="recipeingredient",
            name="is_pantry",
            field=models.BooleanField(
                default=False,
                help_text="По умолчанию не включается в корзину.",
                verbose_name="приправа, специя или продукт из запасов",
            ),
        ),
        migrations.AddField(
            model_name="recipestep",
            name="section",
            field=models.CharField(
                blank=True,
                help_text="Например, «Суп» или «Гренки».",
                max_length=120,
                verbose_name="часть блюда",
            ),
        ),
        migrations.AddField(
            model_name="importjob",
            name="custom_prompt",
            field=models.TextField(
                blank=True,
                help_text="Дополнительные требования к адаптации рецепта.",
                verbose_name="пожелания к импорту",
            ),
        ),
        migrations.AddField(
            model_name="importjob",
            name="recipes",
            field=models.ManyToManyField(
                blank=True,
                related_name="import_jobs",
                to="recipes.recipe",
                verbose_name="созданные рецепты",
            ),
        ),
        migrations.RunPython(link_existing_import_recipes, migrations.RunPython.noop),
        migrations.RunPython(
            classify_existing_pantry_ingredients,
            migrations.RunPython.noop,
        ),
    ]

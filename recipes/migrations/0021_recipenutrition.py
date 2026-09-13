from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models

from recipes.importing.normalizer import estimate_nutrition

NUTRITION_FIELDS = (
    "calories_per_serving",
    "proteins_per_serving",
    "fats_per_serving",
    "carbohydrates_per_serving",
    "calories_per_100g",
    "proteins_per_100g",
    "fats_per_100g",
    "carbohydrates_per_100g",
)
ESTIMATE_NOTE = (
    "Приблизительная оценка по сырым ингредиентам без учёта потерь при готовке."
)


def copy_nutrition_forward(apps, schema_editor):
    Recipe = apps.get_model("recipes", "Recipe")
    RecipeNutrition = apps.get_model("recipes", "RecipeNutrition")
    for recipe in Recipe.objects.all().prefetch_related("ingredients"):
        manual = set(recipe.nutrition_manual_fields or [])
        values = {field: getattr(recipe, field) for field in NUTRITION_FIELDS}
        missing = [field for field in NUTRITION_FIELDS if values[field] is None]
        notes = ""
        if missing:
            estimated = estimate_nutrition(
                [
                    {
                        "name": ingredient.name,
                        "quantity": (
                            str(ingredient.quantity)
                            if ingredient.quantity is not None
                            else None
                        ),
                        "unit": ingredient.unit,
                    }
                    for ingredient in recipe.ingredients.all()
                ],
                recipe.servings,
            )
            for field in missing:
                values[field] = (
                    Decimal(estimated[field])
                    if estimated[field] is not None
                    else Decimal("0")
                )
            notes = ESTIMATE_NOTE
        if manual.issuperset(NUTRITION_FIELDS):
            source = "manual"
        else:
            source = "estimated"
        RecipeNutrition.objects.create(
            recipe=recipe,
            source=source,
            notes=notes,
            manual_fields=sorted(manual),
            **values,
        )


def copy_nutrition_backward(apps, schema_editor):
    RecipeNutrition = apps.get_model("recipes", "RecipeNutrition")
    for nutrition in RecipeNutrition.objects.select_related("recipe"):
        recipe = nutrition.recipe
        for field in NUTRITION_FIELDS:
            setattr(recipe, field, getattr(nutrition, field))
        recipe.nutrition_manual_fields = list(nutrition.manual_fields or [])
        recipe.calories_estimated = nutrition.source != "manual"
        recipe.save()


def nutrition_field(label):
    return models.DecimalField(
        decimal_places=1,
        max_digits=8,
        validators=[django.core.validators.MinValueValidator(Decimal("0"))],
        verbose_name=label,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("recipes", "0020_recipe_source_links"),
    ]

    operations = [
        migrations.CreateModel(
            name="RecipeNutrition",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("calories_per_serving", nutrition_field("ккал на порцию")),
                ("proteins_per_serving", nutrition_field("белки на порцию, г")),
                ("fats_per_serving", nutrition_field("жиры на порцию, г")),
                ("carbohydrates_per_serving", nutrition_field("углеводы на порцию, г")),
                ("calories_per_100g", nutrition_field("ккал на 100 г")),
                ("proteins_per_100g", nutrition_field("белки на 100 г")),
                ("fats_per_100g", nutrition_field("жиры на 100 г")),
                ("carbohydrates_per_100g", nutrition_field("углеводы на 100 г")),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("ai", "рассчитано Гермесом"),
                            ("estimated", "оценено по ингредиентам"),
                            ("manual", "указано вручную"),
                        ],
                        default="estimated",
                        max_length=16,
                        verbose_name="источник расчёта",
                    ),
                ),
                (
                    "notes",
                    models.TextField(
                        blank=True,
                        help_text="Допущения расчёта: слитое масло, удалённые кости, уварка и т. п.",
                        verbose_name="как считали",
                    ),
                ),
                ("manual_fields", models.JSONField(default=list, editable=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "recipe",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="nutrition",
                        to="recipes.recipe",
                    ),
                ),
            ],
            options={
                "verbose_name": "КБЖУ",
                "verbose_name_plural": "КБЖУ",
            },
        ),
        migrations.RunPython(copy_nutrition_forward, copy_nutrition_backward),
        *[
            migrations.RemoveField(model_name="recipe", name=field)
            for field in (
                *NUTRITION_FIELDS,
                "calories_estimated",
                "nutrition_manual_fields",
            )
        ],
    ]

import re
from decimal import Decimal
from typing import Any

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


# Frozen copy of the local nutrition estimator (recipes.importing.normalizer and
# recipes.models) at the time of this migration, so the backfill never depends
# on later changes to the app code.
def is_water_ingredient_name(name: str) -> bool:
    """Return whether an ingredient is plain water rather than a food product."""
    words = set(re.findall(r"[a-zа-я]+", (name or "").lower().replace("ё", "е")))
    water_words = {
        "вода",
        "воды",
        "воду",
        "water",
        "кипяток",
        "кипятка",
        "лед",
        "льда",
        "ice",
    }
    qualifiers = {
        "горячая",
        "горячей",
        "горячую",
        "холодная",
        "холодной",
        "холодную",
        "теплая",
        "теплой",
        "теплую",
        "питьевая",
        "питьевой",
        "питьевую",
        "фильтрованная",
        "фильтрованной",
        "фильтрованную",
        "кипяченая",
        "кипяченой",
        "кипяченую",
        "ледяная",
        "ледяной",
        "ледяную",
        "газированная",
        "газированной",
        "газированную",
        "минеральная",
        "минеральной",
        "минеральную",
        "комнатная",
        "комнатной",
        "комнатную",
        "температура",
        "температуры",
        "кубик",
        "кубики",
        "кубиках",
        "колотый",
        "колотого",
        "в",
        "из",
    }
    return bool(words & water_words) and words <= water_words | qualifiers


NUTRITION_PROFILES: tuple[tuple[re.Pattern[str], tuple[Decimal, ...]], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), tuple(Decimal(item) for item in values))
    for pattern, values in (
        (r"масло (?:растительн|оливков|подсолнечн)", ("884", "0", "100", "0")),
        (r"масло сливочн", ("748", "0.5", "82", "0.8")),
        (r"мука", ("334", "10.3", "1.1", "70.6")),
        (r"сахар", ("387", "0", "0", "100")),
        (r"мед", ("304", "0.3", "0", "82.4")),
        (r"макарон|паста|спагетти", ("350", "12", "1.5", "72")),
        (r"рис", ("344", "6.7", "0.7", "78.9")),
        (r"греч", ("343", "13.3", "3.4", "71.5")),
        (r"овсян", ("370", "13", "6.5", "62")),
        (r"картоф", ("77", "2", "0.4", "16.3")),
        (r"морков", ("41", "0.9", "0.2", "9.6")),
        (r"лук", ("40", "1.1", "0.1", "9.3")),
        (r"чеснок", ("149", "6.4", "0.5", "33.1")),
        (r"томат|помидор", ("18", "0.9", "0.2", "3.9")),
        (r"огур", ("15", "0.7", "0.1", "3.6")),
        (r"капуст", ("25", "1.3", "0.1", "5.8")),
        (r"гриб|шампиньон", ("27", "4.3", "1", "0.1")),
        (r"горох|чечевиц|фасол", ("330", "23", "1.5", "57")),
        (r"куриц|индейк", ("165", "31", "3.6", "0")),
        (r"говядин", ("250", "26", "17", "0")),
        (r"свинин", ("242", "27", "14", "0")),
        (r"рыб|лосос|семг|треск", ("160", "22", "8", "0")),
        (r"яйц", ("155", "12.6", "10.6", "1.1")),
        (r"молок", ("60", "3.2", "3.2", "4.7")),
        (r"сливк", ("205", "2.8", "20", "3.2")),
        (r"сметан", ("200", "2.5", "20", "3.4")),
        (r"творог", ("121", "17", "5", "1.8")),
        (r"сыр", ("350", "25", "27", "2")),
        (r"хлеб|батон|булк", ("255", "8", "3", "49")),
        (r"яблок", ("52", "0.3", "0.2", "13.8")),
        (r"банан", ("89", "1.1", "0.3", "22.8")),
        (r"орех", ("620", "18", "59", "18")),
        (r"шоколад", ("540", "7", "32", "59")),
    )
)

PIECE_WEIGHTS: tuple[tuple[re.Pattern[str], Decimal], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), Decimal(grams))
    for pattern, grams in (
        (r"яйц", "55"),
        (r"лук", "100"),
        (r"морков", "90"),
        (r"картоф", "150"),
        (r"яблок", "170"),
        (r"банан", "120"),
        (r"чеснок", "5"),
        (r"помидор|томат", "120"),
    )
)


def _ingredient_grams(name: str, quantity: str | None, unit: str) -> Decimal | None:
    if quantity is None:
        return None
    amount = Decimal(quantity)
    normalized = unit.lower().replace(" ", "").replace("ё", "е")
    if normalized in {"г", "гр", "g", "мл", "ml"}:
        return amount
    if normalized in {"кг", "kg", "л", "l"}:
        return amount * 1000
    if normalized in {"ст.л.", "ст.л", "стл", "tbsp"}:
        return amount * 15
    if normalized in {"ч.л.", "ч.л", "чл", "tsp"}:
        return amount * 5
    if normalized in {"шт.", "шт", "штук", "piece", "pcs"}:
        for pattern, grams in PIECE_WEIGHTS:
            if pattern.search(name):
                return amount * grams
    return None


def estimate_nutrition(ingredients: list[dict[str, Any]], servings: int) -> dict[str, str | None]:
    total_grams = Decimal("0")
    food_grams = Decimal("0")
    recognized_grams = Decimal("0")
    totals = [Decimal("0") for _ in range(4)]
    for ingredient in ingredients:
        grams = _ingredient_grams(
            ingredient["name"], ingredient["quantity"], ingredient["unit"]
        )
        if grams is None:
            continue
        total_grams += grams
        if is_water_ingredient_name(ingredient["name"]):
            continue
        food_grams += grams
        for pattern, profile in NUTRITION_PROFILES:
            if pattern.search(ingredient["name"]):
                recognized_grams += grams
                for index, value_per_100g in enumerate(profile):
                    totals[index] += grams * value_per_100g / 100
                break
    names = ("calories", "proteins", "fats", "carbohydrates")
    result = {f"{name}_per_serving": None for name in names}
    result.update({f"{name}_per_100g": None for name in names})
    if totals[0] <= 0 or food_grams <= 0 or recognized_grams / food_grams < Decimal("0.5"):
        return result
    for name, total in zip(names, totals):
        result[f"{name}_per_serving"] = str(
            (total / max(1, servings)).quantize(Decimal("0.1"))
        )
        if total_grams:
            result[f"{name}_per_100g"] = str(
                (total * 100 / total_grams).quantize(Decimal("0.1"))
            )
    return result

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

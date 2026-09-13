"""Работа с обязательным КБЖУ рецепта.

Каждый рецепт должен иметь связанную запись ``RecipeNutrition``. Значения
приходят из трёх источников: расчёт Гермеса при импорте или переработке,
локальная оценка по ингредиентам и ручной ввод. Ручные значения помечаются в
``manual_fields`` и переживают любые автоматические пересчёты.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from .importing.normalizer import estimate_nutrition
from .models import NUTRITION_FIELDS, Recipe, RecipeIngredient, RecipeNutrition

ESTIMATE_NOTE = (
    "Приблизительная оценка по сырым ингредиентам без учёта потерь при готовке."
)
UNKNOWN_NOTE = "Состав не удалось оценить автоматически; укажите КБЖУ вручную."


def _ingredient_payload(ingredients: Iterable[RecipeIngredient]) -> list[dict[str, Any]]:
    return [
        {
            "name": ingredient.name,
            "quantity": (
                str(ingredient.quantity) if ingredient.quantity is not None else None
            ),
            "unit": ingredient.unit,
        }
        for ingredient in ingredients
    ]


def estimate_for_recipe(
    recipe: Recipe, ingredients: Iterable[RecipeIngredient] | None = None
) -> dict[str, Decimal]:
    """Локальная оценка всех восьми значений; неизвестное заменяется нулём."""
    items = list(ingredients if ingredients is not None else recipe.ingredients.all())
    estimated = estimate_nutrition(_ingredient_payload(items), recipe.servings)
    return {
        field: Decimal(estimated[field]) if estimated[field] is not None else Decimal("0")
        for field in NUTRITION_FIELDS
    }


def _source_for(manual: set[str], automatic: str) -> str:
    if manual.issuperset(NUTRITION_FIELDS):
        return RecipeNutrition.Source.MANUAL
    return automatic


def _fill_estimates(
    nutrition: RecipeNutrition,
    recipe: Recipe,
    ingredients: Iterable[RecipeIngredient] | None,
    fields: list[str],
) -> None:
    """Подставить локальную оценку в указанные поля и пометить запись."""
    if not fields:
        return
    estimated = estimate_for_recipe(recipe, ingredients)
    for field in fields:
        setattr(nutrition, field, estimated[field])
    nutrition.source = RecipeNutrition.Source.ESTIMATED
    nutrition.notes = (
        ESTIMATE_NOTE if any(estimated[field] for field in NUTRITION_FIELDS) else UNKNOWN_NOTE
    )


def ensure_nutrition(
    recipe: Recipe, ingredients: Iterable[RecipeIngredient] | None = None
) -> RecipeNutrition:
    """Вернуть КБЖУ рецепта, создав локальную оценку, если записи ещё нет."""
    nutrition = recipe.nutrition_or_none
    if nutrition is not None:
        return nutrition
    nutrition = RecipeNutrition(recipe=recipe)
    _fill_estimates(nutrition, recipe, ingredients, list(NUTRITION_FIELDS))
    nutrition.save()
    recipe.nutrition = nutrition
    return nutrition


def apply_imported_nutrition(
    recipe: Recipe,
    data: dict[str, Any] | None,
    ingredients: Iterable[RecipeIngredient] | None = None,
) -> RecipeNutrition:
    """Записать КБЖУ из нормализованного ответа модели.

    Поля, которые пользователь ранее ввёл вручную, сохраняются как есть; всё,
    чего модель не указала, дозаполняется локальной оценкой.
    """
    nutrition = recipe.nutrition_or_none or RecipeNutrition(recipe=recipe)
    manual = set(nutrition.manual_fields or []) if nutrition.pk else set()
    values = data or {}
    missing: list[str] = []
    for field in NUTRITION_FIELDS:
        if field in manual:
            continue
        raw = values.get(field)
        if raw is None:
            missing.append(field)
        else:
            setattr(nutrition, field, Decimal(str(raw)))
    if manual.issuperset(NUTRITION_FIELDS):
        nutrition.source = RecipeNutrition.Source.MANUAL
    elif missing or values.get("source") != RecipeNutrition.Source.AI:
        _fill_estimates(nutrition, recipe, ingredients, missing)
        nutrition.source = RecipeNutrition.Source.ESTIMATED
        if not missing:
            nutrition.notes = str(values.get("notes") or ESTIMATE_NOTE)
    else:
        nutrition.source = RecipeNutrition.Source.AI
        nutrition.notes = str(values.get("notes") or "")
    nutrition.manual_fields = sorted(manual)
    nutrition.save()
    recipe.nutrition = nutrition
    return nutrition


def save_form_nutrition(
    recipe: Recipe,
    submitted: dict[str, Decimal | None],
    *,
    changed_fields: set[str],
    notes: str | None,
    recalculate: bool,
    ingredients: Iterable[RecipeIngredient] | None = None,
) -> RecipeNutrition:
    """Сохранить КБЖУ из формы рецепта.

    ``submitted`` — значения восьми полей (``None`` для пустых),
    ``changed_fields`` — какие из них пользователь изменил в этой отправке,
    ``notes`` — новый текст пояснения или ``None``, если его не трогали,
    ``recalculate`` — пересчитать автоматические поля из-за изменения состава
    или числа порций. Правка одного поля вручную не трогает остальные: расчёт
    Гермеса точнее локальной оценки.
    """
    nutrition = recipe.nutrition_or_none
    creating = nutrition is None
    if creating:
        nutrition = RecipeNutrition(recipe=recipe)
        manual: set[str] = set()
        changed_fields = {
            field for field in NUTRITION_FIELDS if submitted.get(field) is not None
        }
    else:
        manual = set(nutrition.manual_fields or [])
    cleared: list[str] = []
    for field in changed_fields:
        if submitted.get(field) is None:
            manual.discard(field)
            cleared.append(field)
        else:
            manual.add(field)
            setattr(nutrition, field, submitted[field])
    if creating or recalculate:
        _fill_estimates(
            nutrition,
            recipe,
            ingredients,
            [field for field in NUTRITION_FIELDS if field not in manual],
        )
    elif cleared:
        _fill_estimates(nutrition, recipe, ingredients, cleared)
    if notes is not None:
        nutrition.notes = notes
    nutrition.manual_fields = sorted(manual)
    nutrition.source = _source_for(manual, nutrition.source)
    nutrition.save()
    recipe.nutrition = nutrition
    return nutrition

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from recipes.categories import CATEGORY_SLUGS

from .exceptions import AIResponseError


def _text(value: Any, limit: int, default: str = "") -> str:
    if value is None:
        return default
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


def _integer(value: Any, default: int, maximum: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(0, min(number, maximum))


def _quantity(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    try:
        number = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if number < 0 or number >= Decimal("10000000"):
        return None
    return str(number.quantize(Decimal("0.01")))


def normalize_recipe(
    value: Any,
    *,
    require_quantities: bool = False,
    keep_ingredient_notes: bool = True,
    require_categories: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AIResponseError("Модель вернула рецепт в неожиданном формате.")
    title = _text(value.get("title"), 180)
    raw_ingredients = value.get("ingredients")
    raw_steps = value.get("steps")
    if not title or not isinstance(raw_ingredients, list) or not isinstance(raw_steps, list):
        raise AIResponseError("В ответе модели нет названия, ингредиентов или шагов.")

    ingredients = []
    for item in raw_ingredients[:80]:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), 180)
        if not name:
            continue
        quantity = _quantity(item.get("quantity"))
        unit = _text(item.get("unit"), 40)
        if require_quantities and (quantity is None or Decimal(quantity) <= 0 or not unit):
            raise AIResponseError(
                f"Модель не указала измеримое количество для ингредиента «{name}»."
            )
        ingredients.append(
            {
                "section": _text(item.get("section"), 120),
                "name": name,
                "quantity": quantity,
                "unit": unit,
                "note": _text(item.get("note"), 240) if keep_ingredient_notes else "",
                "search_query": _text(item.get("search_query"), 240, name),
                "optional": bool(item.get("optional", False)),
                "estimated": bool(item.get("estimated", False)),
            }
        )

    steps = []
    for item in raw_steps[:60]:
        if isinstance(item, str):
            instruction = _text(item, 5000)
            step_title = ""
        elif isinstance(item, dict):
            instruction = _text(item.get("instruction"), 5000)
            step_title = _text(item.get("title"), 180)
        else:
            continue
        if instruction:
            steps.append({"title": step_title, "instruction": instruction})

    if not ingredients or not steps:
        raise AIResponseError("Модель не смогла выделить ингредиенты или шаги приготовления.")
    raw_categories = value.get("categories", [])
    if not isinstance(raw_categories, list):
        raw_categories = []
    categories = []
    for item in raw_categories:
        slug = _text(item, 60).lower()
        if slug in CATEGORY_SLUGS and slug not in categories:
            categories.append(slug)
    categories = categories[:3]
    if "soup" in categories and "main-course" in categories:
        categories.remove("main-course")
    if require_categories and not categories:
        raise AIResponseError("Модель не выбрала ни одной допустимой категории рецепта.")
    return {
        "title": title,
        "description": _text(value.get("description"), 2000),
        "servings": max(1, _integer(value.get("servings"), 2, 100)),
        "prep_minutes": _integer(value.get("prep_minutes"), 0, 1440),
        "cook_minutes": _integer(value.get("cook_minutes"), 0, 10080),
        "categories": categories,
        "ingredients": ingredients,
        "steps": steps,
    }

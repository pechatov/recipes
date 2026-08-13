from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from recipes.categories import CATEGORY_SLUGS
from recipes.models import is_water_ingredient_name

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


def _optional_integer(value: Any, maximum: int) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    if number < 0 or number > maximum:
        return None
    return number


def _quantity(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if number < 0 or number >= Decimal("10000000"):
        return None
    return str(number.quantize(Decimal("0.01")))


def _nutrient(value: Any, units: str = r"г|g") -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    match = re.fullmatch(
        rf"\s*(\d+(?:[.,]\d+)?)\s*(?:{units})?\s*",
        str(value),
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        number = Decimal(match.group(1).replace(",", "."))
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or number >= Decimal("1000000"):
        return None
    return str(number.quantize(Decimal("0.1")))


def _calories(value: Any) -> str | None:
    return _nutrient(value, r"ккал|kcal")


PANTRY_PATTERN = re.compile(
    r"(?:^|\s)(?:"
    r"соль|перец|паприк|куркум|кориандр|карри|зир|кумин|орегано|базилик|"
    r"тимьян|розмарин|лавров|корица|гвоздик|мускат|ванил|шафран|"
    r"сахар|сода|разрыхлител|крахмал|желатин|дрожж|уксус|"
    r"масло растительн|масло оливков|соевый соус|горчиц|мед|"
    r"чеснок сушен|лук сушен|специ|приправ|seasoning|spice|salt|pepper"
    r")",
    re.IGNORECASE,
)


def _is_pantry(item: dict[str, Any], name: str, quantity: str | None, unit: str) -> bool:
    if quantity is None:
        return bool(item.get("is_pantry", False)) or bool(
            PANTRY_PATTERN.search(name.replace("ё", "е"))
        )
    amount = Decimal(quantity)
    normalized_unit = unit.lower().replace(" ", "").replace("ё", "е")
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
    threshold = thresholds.get(normalized_unit)
    if threshold is not None and amount > threshold:
        return False
    if bool(item.get("is_pantry", False)) or PANTRY_PATTERN.search(name.replace("ё", "е")):
        return True
    # Tiny quantities expressed as spoons/pinches are normally cupboard staples.
    return normalized_unit in {"щепотка", "щепотки", "ч.л.", "чл", "tsp"} and amount <= 3


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


def estimate_calories(
    ingredients: list[dict[str, Any]], servings: int
) -> tuple[str | None, str | None]:
    nutrition = estimate_nutrition(ingredients, servings)
    return nutrition["calories_per_serving"], nutrition["calories_per_100g"]


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

    all_ingredients = []
    for item in raw_ingredients[:80]:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), 180)
        if not name:
            continue
        quantity = _quantity(item.get("quantity"))
        unit = _text(item.get("unit"), 40)
        if (
            require_quantities
            and not is_water_ingredient_name(name)
            and (quantity is None or Decimal(quantity) <= 0 or not unit)
        ):
            raise AIResponseError(
                f"Модель не указала измеримое количество для ингредиента «{name}»."
            )
        all_ingredients.append(
            {
                "section": _text(item.get("section"), 120),
                "name": name,
                "quantity": quantity,
                "unit": unit,
                "note": _text(item.get("note"), 240) if keep_ingredient_notes else "",
                "search_query": _text(item.get("search_query"), 240, name),
                "optional": bool(item.get("optional", False)),
                "estimated": bool(item.get("estimated", False)),
                "is_pantry": _is_pantry(item, name, quantity, unit),
            }
        )

    ingredients = [
        ingredient
        for ingredient in all_ingredients
        if not is_water_ingredient_name(ingredient["name"])
    ]

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
            steps.append(
                {
                    "section": _text(item.get("section"), 120) if isinstance(item, dict) else "",
                    "title": step_title,
                    "instruction": instruction,
                    "image_url": (
                        _text(item.get("image_url"), 2048)
                        if isinstance(item, dict)
                        else ""
                    ),
                    "video_timestamp_seconds": (
                        _optional_integer(item.get("video_timestamp_seconds"), 604800)
                        if isinstance(item, dict)
                        else None
                    ),
                }
            )

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
    servings = max(1, _integer(value.get("servings"), 2, 100))
    estimated_nutrition = estimate_nutrition(all_ingredients, servings)
    return {
        "title": title,
        "description": _text(value.get("description"), 2000),
        "servings": servings,
        "prep_minutes": _integer(value.get("prep_minutes"), 0, 1440),
        "cook_minutes": _integer(value.get("cook_minutes"), 0, 10080),
        "categories": categories,
        "calories_per_serving": (
            _calories(value.get("calories_per_serving"))
            or estimated_nutrition["calories_per_serving"]
        ),
        "calories_per_100g": (
            _calories(value.get("calories_per_100g"))
            or estimated_nutrition["calories_per_100g"]
        ),
        **{
            field: _nutrient(value.get(field)) or estimated_nutrition[field]
            for field in (
                "proteins_per_serving", "fats_per_serving",
                "carbohydrates_per_serving", "proteins_per_100g", "fats_per_100g",
                "carbohydrates_per_100g",
            )
        },
        "cover_image_url": _text(value.get("cover_image_url"), 2048),
        "cover_image_search_query": _text(
            value.get("cover_image_search_query"), 200
        ),
        "ingredients": ingredients,
        "steps": steps,
    }


def normalize_recipes(value: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Normalize both the legacy single recipe and the new multi-recipe envelope."""
    if isinstance(value, dict) and "recipes" in value:
        raw_recipes = value.get("recipes")
    elif isinstance(value, list):
        raw_recipes = value
    else:
        raw_recipes = [value]
    if not isinstance(raw_recipes, list) or not raw_recipes:
        raise AIResponseError("Модель не вернула ни одного рецепта.")
    recipes = [normalize_recipe(recipe, **kwargs) for recipe in raw_recipes[:12]]
    if not recipes:
        raise AIResponseError("Модель не вернула ни одного рецепта.")
    return recipes

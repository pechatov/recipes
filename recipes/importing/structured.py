from __future__ import annotations

import re
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

from recipes.categories import infer_category_slugs

from .normalizer import normalize_recipe


def _plain(value: Any) -> str:
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    return BeautifulSoup(unescape(str(value or "")), "html.parser").get_text(" ", strip=True)


def _duration_minutes(value: Any) -> int:
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", str(value or ""))
    if not match:
        return 0
    days, hours, minutes = (int(part or 0) for part in match.groups())
    return days * 1440 + hours * 60 + minutes


def _servings(value: Any) -> int:
    if isinstance(value, list):
        value = value[0] if value else ""
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else 2


def _ingredient(value: Any) -> dict[str, Any]:
    text = _plain(value)
    match = re.match(
        r"^\s*(?P<quantity>\d+(?:[.,]\d+)?|\d+\s*/\s*\d+)?\s*"
        r"(?P<unit>кг|г|мг|л|мл|шт\.?|ч\.\s*л\.?|ст\.\s*л\.?)?\s*(?P<name>.+)$",
        text,
        re.IGNORECASE,
    )
    if not match:
        return {
            "section": "",
            "name": text,
            "quantity": None,
            "unit": "",
            "note": "",
            "search_query": text,
            "estimated": False,
        }
    quantity = match.group("quantity")
    if quantity and "/" in quantity:
        numerator, denominator = quantity.replace(" ", "").split("/", 1)
        quantity = str(float(numerator) / float(denominator))
    name = match.group("name").strip(" ,-–")
    return {
        "section": "",
        "name": name,
        "quantity": quantity,
        "unit": match.group("unit") or "",
        "note": "",
        "search_query": name,
        "optional": "по желанию" in name.lower(),
        "estimated": False,
    }


def _steps(value: Any, section: str = "") -> list[dict[str, str]]:
    if not isinstance(value, list):
        value = [value]
    result = []
    for item in value:
        if isinstance(item, dict) and str(item.get("@type", "")).lower() == "howtosection":
            result.extend(
                _steps(item.get("itemListElement", []), _plain(item.get("name")) or section)
            )
            continue
        if isinstance(item, dict):
            instruction = _plain(item.get("text") or item.get("name"))
            title = _plain(item.get("name")) if item.get("text") else ""
        else:
            instruction = _plain(item)
            title = ""
        if instruction:
            result.append({"section": section, "title": title, "instruction": instruction})
    return result


def _nutrition_calories(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", str(value.get("calories") or ""))
    return match.group().replace(",", ".") if match else None


def adapt_structured_recipe(value: dict[str, Any]) -> dict[str, Any]:
    ingredients = [_ingredient(item) for item in value.get("recipeIngredient", [])]
    title = _plain(value.get("name"))
    description = _plain(value.get("description"))
    data = {
        "title": title,
        "description": description,
        "servings": _servings(value.get("recipeYield")),
        "prep_minutes": _duration_minutes(value.get("prepTime")),
        "cook_minutes": _duration_minutes(value.get("cookTime") or value.get("totalTime")),
        "calories_per_serving": _nutrition_calories(value.get("nutrition")),
        "categories": infer_category_slugs(f"{title} {description}"),
        "ingredients": ingredients,
        "steps": _steps(value.get("recipeInstructions", [])),
    }
    return normalize_recipe(data)

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from .exceptions import UnsafeSourceError
from .extractors import SourceDocument


# These expressions intentionally require references to prompts, roles, or
# instructions. Phrases such as "ignore the previous batch" in a real recipe
# must not be mistaken for prompt injection.
INJECTION_PATTERNS = (
    re.compile(
        r"\b(?:ignore|disregard|forget|override)\b.{0,80}"
        r"\b(?:previous|prior|above|system|developer)\b.{0,40}"
        r"\b(?:instructions?|prompts?|messages?|rules?)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:reveal|show|print|repeat|expose|leak)\b.{0,60}"
        r"\b(?:system|developer|hidden)\b.{0,30}"
        r"\b(?:prompts?|instructions?|messages?|rules?)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:игнорируй|игнорировать|забудь|отмени|переопредели)\w*\b.{0,80}"
        r"\b(?:предыдущ|системн|скрыт|инструкц|промпт|сообщен|правил)\w*\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:покажи|раскрой|выведи|повтори|напечатай)\w*\b.{0,60}"
        r"\b(?:системн|скрыт)\w*\b.{0,30}"
        r"\b(?:промпт|инструкц|сообщен|правил)\w*\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"(?:<\s*/?\s*(?:system|assistant|developer)\s*>|"
        r"\[\s*(?:system|assistant|developer)\s*\])",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:system|developer|hidden)\b.{0,20}"
        r"\b(?:prompts?|instructions?|messages?|rules?)\b|"
        r"\b(?:системн|скрыт)\w*\b.{0,20}"
        r"\b(?:промпт|инструкц|сообщен|правил)\w*\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:you\s+are\s+now|act\s+as)\b.{0,30}"
        r"\b(?:assistant|ai|language\s+model)\b|"
        r"\bты\s+теперь\b.{0,30}\b(?:ассистент|ии|модель)\w*\b",
        re.IGNORECASE | re.DOTALL,
    ),
)

STRUCTURE_PATTERNS = (
    r"\bрецепт\w*\b",
    r"\bингредиент\w*\b",
    r"\bприготовлен\w*\b",
    r"\bпорци\w*\b",
    r"\brecipe\w*\b",
    r"\bingredient\w*\b",
    r"\bservings?\b",
    r"\bdirections?\b",
    r"\bінгредієнт\w*\b",
    r"\bприготуван\w*\b",
    r"\bпорці\w*\b",
)
ACTION_PATTERNS = (
    r"\b(?:готов|нарез|измельч|смеш|добав|обжар|жар|вар|выпек|запек|туш|"
    r"взби|замес|очист|посол|приправ|разогре|пода)\w*\b",
    r"\b(?:cook|chop|slice|mix|add|fry|boil|bake|roast|simmer|whisk|knead|"
    r"peel|season|heat|serve)\w*\b",
    r"\b(?:готув|наріж|подрібн|зміш|дода|обсмаж|смаж|варі|вари|випіка|запіка|"
    r"тушк|збив|заміс|очист|посол|приправ|розігр|пода)\w*\b",
)
INGREDIENT_PATTERNS = (
    r"\b(?:соль|перец|мук\w*|масл\w*|яйц\w*|молок\w*|сыр\w*|картоф\w*|"
    r"лук\w*|чеснок\w*|помидор\w*|томат\w*|морков\w*|мяс\w*|куриц\w*|"
    r"рыб\w*|рис\w*|круп\w*|сахар\w*)\b",
    r"\b(?:salt|pepper|flour|butter|oil|egg|milk|cheese|potato|onion|garlic|"
    r"tomato|carrot|meat|chicken|fish|rice|sugar)\w*\b",
    r"\b(?:сіль|перець|борошн\w*|олія|масл\w*|яйц\w*|молок\w*|сир\w*|"
    r"картопл\w*|цибул\w*|часник\w*|помідор\w*|томат\w*|моркв\w*|м['’]яс\w*|"
    r"курк\w*|риб\w*|рис\w*|круп\w*|цукор\w*|буряк\w*|капуст\w*)\b",
)
MEASUREMENT_PATTERN = re.compile(
    r"(?:\b\d+(?:[.,]\d+)?\s*)?\b(?:г|гр|кг|мл|л|шт|ч\.\s*л|ст\.\s*л|"
    r"грамм\w*|килограмм\w*|миллилитр\w*|литр\w*|ложк\w*|стакан\w*|"
    r"грам\w*|кілограм\w*|мілілітр\w*|літр\w*|склянк\w*|"
    r"g|kg|ml|l|tsp|tbsp|cups?|ounces?|oz|pounds?|lb)\b",
    re.IGNORECASE,
)


def _json_text(values: Iterable[dict[str, Any]]) -> str:
    try:
        return json.dumps(list(values), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return ""


def _source_text(document: SourceDocument) -> str:
    image_urls = (
        *document.cover_image_urls,
        *document.step_image_urls,
        *(url for urls in document.recipe_cover_image_urls for url in urls),
        *(url for urls in document.recipe_step_image_urls for url in urls),
    )
    return "\n".join(
        (
            document.title,
            document.text,
            _json_text(document.all_structured_recipes),
            *image_urls,
        )
    )


def _has_recipe_instructions(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_has_recipe_instructions(item) for item in value)
    if isinstance(value, dict):
        return _has_recipe_instructions(value.get("text") or value.get("itemListElement"))
    return False


def _is_plausible_structured_recipe(recipe: dict[str, Any]) -> bool:
    ingredients = recipe.get("recipeIngredient")
    return bool(
        str(recipe.get("name") or "").strip()
        and isinstance(ingredients, list)
        and any(str(item).strip() for item in ingredients)
        and _has_recipe_instructions(recipe.get("recipeInstructions"))
    )


def _pattern_count(patterns: Iterable[str], text: str) -> int:
    matches = {
        match.group(0).casefold()
        for pattern in patterns
        for match in re.finditer(pattern, text, re.IGNORECASE)
    }
    return len(matches)


def _looks_like_cooking(document: SourceDocument, text: str) -> bool:
    if any(_is_plausible_structured_recipe(item) for item in document.all_structured_recipes):
        return True

    structure_hits = _pattern_count(STRUCTURE_PATTERNS, text)
    action_hits = _pattern_count(ACTION_PATTERNS, text)
    ingredient_hits = _pattern_count(INGREDIENT_PATTERNS, text)
    has_measurement = bool(MEASUREMENT_PATTERN.search(text))
    return (
        structure_hits >= 1
        and action_hits >= 1
        and (ingredient_hits >= 1 or has_measurement)
    ) or (
        action_hits >= 2
        and ingredient_hits >= 2
        and (has_measurement or action_hits >= 3)
    )


def validate_source_safety(document: SourceDocument) -> None:
    """Reject off-topic and adversarial sources before sending them to an LLM."""
    text = _source_text(document)
    if any(pattern.search(text) for pattern in INJECTION_PATTERNS):
        raise UnsafeSourceError(
            "Импорт заблокирован: в источнике обнаружены инструкции, похожие на "
            "prompt injection."
        )
    if not _looks_like_cooking(document, text):
        raise UnsafeSourceError(
            "Импорт заблокирован: источник не похож на материал о приготовлении еды."
        )

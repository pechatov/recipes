from __future__ import annotations

import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any


TOKEN_PATTERN = re.compile(r"[a-zа-яё0-9]+(?:[.,][0-9]+)?", re.IGNORECASE)
COUNT_UNIT_PATTERN = r"шт(?:\.|ук(?:а|и)?)?|pcs?|pieces?"
AMOUNT_PATTERN = re.compile(
    r"(?P<amount>[0-9]+(?:[.,][0-9]+)?)\s*"
    rf"(?P<unit>кг|гр?\.?|kg|g|мл|ml|л|l|{COUNT_UNIT_PATTERN})\b",
    re.IGNORECASE,
)
MULTIPACK_PATTERN = re.compile(
    r"(?P<count>[0-9]+)\s*[xх×*]\s*"
    r"(?P<amount>[0-9]+(?:[.,][0-9]+)?)\s*"
    rf"(?P<unit>кг|гр?\.?|kg|g|мл|ml|л|l|{COUNT_UNIT_PATTERN})\b",
    re.IGNORECASE,
)
UNIT_KIND_AND_FACTOR = {
    "г": ("mass", Decimal("1")),
    "гр": ("mass", Decimal("1")),
    "g": ("mass", Decimal("1")),
    "кг": ("mass", Decimal("1000")),
    "kg": ("mass", Decimal("1000")),
    "мл": ("volume", Decimal("1")),
    "ml": ("volume", Decimal("1")),
    "л": ("volume", Decimal("1000")),
    "l": ("volume", Decimal("1000")),
    "шт": ("count", Decimal("1")),
    "pc": ("count", Decimal("1")),
    "pcs": ("count", Decimal("1")),
    "piece": ("count", Decimal("1")),
    "pieces": ("count", Decimal("1")),
}
IGNORED_TOKENS = {
    "в",
    "для",
    "и",
    "из",
    "на",
    "по",
    "с",
    "со",
    "шт",
    "штук",
    "штука",
    "штуки",
    "g",
    "kg",
    "l",
    "ml",
}
# These words materially change a grocery item. An unexpected modifier should
# not silently turn a generic query into an "exact" match.
SENSITIVE_MODIFIERS = {
    "безлактозн",
    "безглютен",
    "козий",
    "козье",
    "кокосов",
    "обезжир",
    "овсян",
    "рисов",
    "соев",
    "цельнозерн",
}


def _normalized_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return " ".join(text.split())


def _tokens(value: Any) -> list[str]:
    return [
        token.replace(",", ".")
        for token in TOKEN_PATTERN.findall(_normalized_text(value))
        if token not in IGNORED_TOKENS
    ]


def _word_matches(left: str, right: str) -> bool:
    if left == right:
        return True
    if left.replace(".", "", 1).isdigit() or right.replace(".", "", 1).isdigit():
        return False
    # A short prefix handles common Russian inflections without pretending to
    # be a full morphological analyser.
    return min(len(left), len(right)) >= 5 and left[:5] == right[:5]


def _contains_token(tokens: list[str], expected: str) -> bool:
    return any(_word_matches(token, expected) for token in tokens)


def _decimal(value: Any) -> Decimal | None:
    raw = str(value).strip().replace(",", ".")
    if len(raw) > 24 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
        return None
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if (
        not number.is_finite()
        or number < Decimal("0.001")
        or number > Decimal("1000000")
    ):
        return None
    return number


def _unit(value: Any) -> tuple[str, Decimal] | None:
    normalized = _normalized_text(value).rstrip(".")
    aliases = {
        "грамм": "г",
        "грамма": "г",
        "граммов": "г",
        "килограмм": "кг",
        "килограмма": "кг",
        "килограммов": "кг",
        "миллилитр": "мл",
        "миллилитра": "мл",
        "миллилитров": "мл",
        "литр": "л",
        "литра": "л",
        "литров": "л",
        "штука": "шт",
        "штуки": "шт",
        "штук": "шт",
    }
    return UNIT_KIND_AND_FACTOR.get(aliases.get(normalized, normalized))


def _package_amount(candidate: dict[str, Any]) -> tuple[str, Decimal] | None:
    for source in (candidate.get("weight"), candidate.get("name")):
        normalized = _normalized_text(source)
        multipack = MULTIPACK_PATTERN.search(normalized)
        match = multipack or AMOUNT_PATTERN.search(normalized)
        if not match:
            continue
        unit = _unit(match.group("unit"))
        amount = _decimal(match.group("amount"))
        if unit and amount:
            kind, factor = unit
            count = _decimal(match.group("count")) if multipack else Decimal("1")
            return kind, amount * factor * (count or Decimal("1"))
    return None


def _required_packages(
    ingredient: dict[str, Any], candidate: dict[str, Any]
) -> tuple[int | None, str]:
    quantity = _decimal(ingredient.get("quantity"))
    requested_unit = _unit(ingredient.get("unit"))
    if quantity is None or requested_unit is None:
        return 1, "Не удалось точно сопоставить единицы; выбрана одна упаковка."
    package = _package_amount(candidate)
    if package is None:
        if requested_unit[0] == "count":
            return 1, "Размер штучной упаковки не указан; выбрана одна упаковка."
        return 1, "Размер упаковки не указан; выбрана одна упаковка."
    requested_kind, requested_factor = requested_unit
    package_kind, package_value = package
    if requested_kind != package_kind:
        return None, ""
    return max(1, math.ceil((quantity * requested_factor) / package_value)), ""


def _available_stock(candidate: dict[str, Any]) -> int | None:
    if candidate.get("available") is False:
        return 0
    value = candidate.get("in_stock", candidate.get("inStock"))
    if isinstance(value, bool):
        return None if value else 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _candidate_score(query_tokens: list[str], candidate: dict[str, Any], rank: int):
    name_tokens = _tokens(candidate.get("name"))
    if not query_tokens or not name_tokens:
        return 0.0, 0.0, []
    matched = [token for token in query_tokens if _contains_token(name_tokens, token)]
    coverage = len(matched) / len(query_tokens)
    unexpected_modifiers = [
        modifier
        for modifier in SENSITIVE_MODIFIERS
        if any(token.startswith(modifier) for token in name_tokens)
        and not any(token.startswith(modifier) for token in query_tokens)
    ]
    query_words = [
        token
        for token in query_tokens
        if not token.replace(".", "", 1).isdigit() and _unit(token) is None
    ]
    if len(query_words) == 1:
        extra_words = [
            token
            for token in name_tokens
            if not token.replace(".", "", 1).isdigit()
            and _unit(token) is None
            and not any(_word_matches(token, query) for query in query_words)
        ]
        if extra_words:
            # A generic one-word query cannot prove that an extra descriptor
            # is merely a brand rather than a different ingredient (for
            # example, "лук" vs "лук-порей"). Require user review.
            unexpected_modifiers.append("additional_descriptor")
    score = coverage * 100 - rank * 1.5 - len(unexpected_modifiers) * 35
    if _contains_token(name_tokens, query_tokens[0]):
        score += 15
    return score, coverage, unexpected_modifiers


def _missing(ingredient: dict[str, Any], warning: str) -> dict[str, Any]:
    return {
        "ingredient_name": str(ingredient.get("name") or "").strip(),
        "requested_quantity": requested_quantity(ingredient),
        "product_id": "",
        "sku_id": "",
        "product_name": "",
        "product_url": "",
        "package_count": 0,
        "added_package_count": 0,
        "quality": "missing",
        "warning": warning,
    }


def requested_quantity(ingredient: dict[str, Any]) -> str:
    quantity = str(ingredient.get("quantity") or "").strip()
    unit = str(ingredient.get("unit") or "").strip()
    return " ".join(part for part in (quantity, unit) if part)


def choose_product(
    ingredient: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Choose one bounded search result and calculate its package count."""
    query = str(
        ingredient.get("search_query") or ingredient.get("name") or ""
    ).strip()
    query_tokens = _tokens(query)
    choices = []
    for rank, candidate in enumerate(candidates[:12]):
        if not isinstance(candidate, dict):
            continue
        product_id = str(candidate.get("product_id") or "").strip()
        sku_id = str(candidate.get("sku_id") or product_id).strip()
        product_name = str(candidate.get("name") or "").strip()
        product_url = str(candidate.get("product_url") or "").strip()
        if not product_id or not sku_id or not product_name or not product_url:
            continue
        package_count, package_warning = _required_packages(ingredient, candidate)
        if package_count is None or package_count > 100:
            continue
        stock = _available_stock(candidate)
        if stock is not None and stock < package_count:
            continue
        score, coverage, modifiers = _candidate_score(query_tokens, candidate, rank)
        choices.append(
            (
                score,
                coverage,
                -rank,
                candidate,
                package_count,
                package_warning,
                modifiers,
            )
        )

    if not choices:
        return _missing(
            ingredient,
            "Подходящий товар не найден или его недостаточно в наличии.",
        )
    score, coverage, _, candidate, package_count, package_warning, modifiers = max(
        choices, key=lambda choice: choice[:3]
    )
    if coverage < 0.45 or score < 35:
        return _missing(ingredient, "Поиск не нашёл достаточно близкого товара.")

    quality = "exact" if coverage >= 0.8 and not modifiers else "substitute"
    warnings = []
    if quality == "substitute":
        warnings.append(
            f"Ближайший вариант к «{query}»; проверьте замену."
        )
    if package_warning:
        warnings.append(package_warning)
        quality = "substitute"
    return {
        "ingredient_name": str(ingredient.get("name") or "").strip(),
        "requested_quantity": requested_quantity(ingredient),
        "product_id": str(candidate.get("product_id") or "").strip(),
        "sku_id": str(candidate.get("sku_id") or candidate.get("product_id") or "").strip(),
        "product_name": str(candidate.get("name") or "").strip(),
        "product_url": str(candidate.get("product_url") or "").strip(),
        "package_count": package_count,
        "added_package_count": 0,
        "quality": quality,
        "warning": " ".join(warnings),
    }

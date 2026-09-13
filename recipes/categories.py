import re

CATEGORY_TAXONOMY = (
    ("breakfast", "Завтрак"),
    ("appetizer", "Закуска"),
    ("soup", "Суп"),
    ("salad", "Салат"),
    ("main-course", "Второе блюдо"),
    ("side-dish", "Гарнир"),
    ("bakery", "Выпечка"),
    ("dessert", "Десерт"),
    ("drink", "Напиток"),
    ("sauce", "Соус"),
    ("preserve", "Заготовка"),
    ("other", "Другое"),
)

CATEGORY_SLUGS = {slug for slug, _ in CATEGORY_TAXONOMY}

# Бейдж «из чего сделано» для вторых блюд.
MAIN_PROTEIN_TAXONOMY = (
    ("chicken", "Курица"),
    ("beef", "Говядина"),
    ("pork", "Свинина"),
    ("fish", "Рыба"),
)

MAIN_PROTEIN_SLUGS = {slug for slug, _ in MAIN_PROTEIN_TAXONOMY}

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
# Бульон, яйца и соусы называют мясо, но блюдо из них не делается.
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


def infer_category_slugs(text: str) -> list[str]:
    value = text.lower()
    rules = (
        ("soup", ("суп", "щи", "борщ", "солянк", "бульон", "уха")),
        ("salad", ("салат",)),
        ("breakfast", ("завтрак", "каша", "омлет", "сырник", "яичниц")),
        ("bakery", ("пирог", "пирож", "хлеб", "булоч", "блин", "олад", "печень")),
        ("dessert", ("десерт", "торт", "крем", "морожен", "конфет")),
        ("drink", ("напит", "компот", "лимонад", "коктейль", "чай")),
        ("salad", ("винегрет",)),
        ("side-dish", ("гарнир", "пюре", "рис", "гречк")),
        ("sauce", ("соус", "заправк", "майонез")),
        ("preserve", ("варенье", "соленье", "маринован", "заготов")),
        ("appetizer", ("закуск", "гренк", "бутерброд", "паштет")),
    )
    matches = [slug for slug, words in rules if any(word in value for word in words)]
    return list(dict.fromkeys(matches))[:3] or ["main-course"]

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

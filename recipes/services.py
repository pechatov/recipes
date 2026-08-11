from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote_plus

from .models import RecipeIngredient, StorePreference, is_water_ingredient_name


LAVKA_SEARCH_URL = "https://lavka.yandex.ru/search?text={query}"
STORE_SEARCH_URLS = {
    StorePreference.Store.AUCHAN: "https://www.auchan.ru/catalog/?q={query}",
    StorePreference.Store.PEREKRESTOK: "https://www.perekrestok.ru/cat/search?search={query}",
    StorePreference.Store.PYATEROCHKA: "https://5ka.ru/search/?q={query}",
    StorePreference.Store.MAGNIT: "https://magnit.ru/search/?q={query}",
    StorePreference.Store.LAVKA: LAVKA_SEARCH_URL,
}


@dataclass(frozen=True)
class ShoppingItem:
    ingredient: RecipeIngredient
    quantity: Decimal | None
    search_url: str
    priority_search_url: str = ""

    @property
    def display_quantity(self) -> str:
        if self.quantity is None:
            return ""
        normalized = self.quantity.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return format(normalized.normalize(), "f")


def build_lavka_search_url(query: str) -> str:
    return LAVKA_SEARCH_URL.format(query=quote_plus(query.strip()))


def build_store_search_url(store: str, query: str) -> str:
    template = STORE_SEARCH_URLS.get(store, LAVKA_SEARCH_URL)
    return template.format(query=quote_plus(query.strip()))


def build_shopping_items(
    recipe, servings: int, preferred_store: str | None = None
) -> list[ShoppingItem]:
    multiplier = Decimal(servings) / Decimal(recipe.servings)
    items = []
    for ingredient in recipe.ingredients.all():
        if is_water_ingredient_name(ingredient.name):
            continue
        quantity = ingredient.quantity * multiplier if ingredient.quantity is not None else None
        items.append(
            ShoppingItem(
                ingredient=ingredient,
                quantity=quantity,
                search_url=build_lavka_search_url(ingredient.effective_search_query),
                priority_search_url=(
                    build_store_search_url(
                        preferred_store, ingredient.effective_search_query
                    )
                    if preferred_store and preferred_store != StorePreference.Store.LAVKA
                    else ""
                ),
            )
        )
    return items


def get_store_preferences(user):
    existing = {item.store: item for item in user.store_preferences.all()}
    defaults = [choice[0] for choice in StorePreference.Store.choices]
    missing = [
        StorePreference(user=user, store=store, position=position)
        for position, store in enumerate(defaults)
        if store not in existing
    ]
    if missing:
        StorePreference.objects.bulk_create(missing)
    return list(user.store_preferences.all().order_by("position", "pk"))

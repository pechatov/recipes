from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote_plus

from django.db import transaction

from .models import RecipeIngredient, StorePreference, is_water_ingredient_name


LAVKA_SEARCH_URL = "https://lavka.yandex.ru/search?text={query}"
YANDEX_EDA_SEARCH_URL = "https://eda.yandex.ru/retail/{brand}/search?{parameters}"
STORE_LINKS = {
    StorePreference.Store.AUCHAN: ("asan_giper", "ashan_w5r8t"),
    StorePreference.Store.PEREKRESTOK: ("perekrestok", ""),
    StorePreference.Store.PYATEROCHKA: ("paterocka", ""),
    StorePreference.Store.MAGNIT: ("magnit_celevaya", ""),
    StorePreference.Store.LAVKA: ("lavka", ""),
}


@dataclass(frozen=True)
class ShoppingItem:
    ingredient: RecipeIngredient
    quantity: Decimal | None
    search_url: str
    selected_store_search_url: str = ""

    @property
    def display_quantity(self) -> str:
        if self.quantity is None:
            return ""
        normalized = self.quantity.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return format(normalized.normalize(), "f")


def build_lavka_search_url(query: str) -> str:
    return LAVKA_SEARCH_URL.format(query=quote_plus(query.strip()))


def build_store_search_url(store: str, query: str) -> str:
    brand, place = STORE_LINKS.get(
        store, STORE_LINKS[StorePreference.Store.LAVKA]
    )
    parameters = []
    if place:
        parameters.append(f"placeSlug={place}")
        parameters.append(f"relatedBrandSlug={brand}")
    parameters.append(f"query={quote_plus(query.strip())}")
    return YANDEX_EDA_SEARCH_URL.format(
        brand=brand,
        parameters="&".join(parameters),
    )


def build_shopping_items(
    recipe, servings: int, selected_store: str | None = None
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
                search_url=build_lavka_search_url(
                    ingredient.effective_search_query
                ),
                selected_store_search_url=build_store_search_url(
                    selected_store or StorePreference.Store.LAVKA,
                    ingredient.effective_search_query,
                ),
            )
        )
    return items


def get_store_preferences(user):
    existing = {item.store: item for item in user.store_preferences.all()}
    defaults = [choice[0] for choice in StorePreference.Store.choices]
    missing = [
        StorePreference(
            user=user,
            store=store,
            position=position,
            enabled=not existing and position == 0,
        )
        for position, store in enumerate(defaults)
        if store not in existing
    ]
    if missing:
        StorePreference.objects.bulk_create(missing)
    return list(user.store_preferences.all().order_by("position", "pk"))


def get_selected_store(user) -> StorePreference:
    preferences = get_store_preferences(user)
    selected = next((item for item in preferences if item.enabled), None)
    if selected is None:
        selected = preferences[0]
        selected.enabled = True
        selected.save(update_fields=["enabled"])
    return selected


def select_store(user, store: str) -> StorePreference:
    with transaction.atomic():
        # Lock the parent row rather than only the existing preferences: this
        # also serializes first-time selection while defaults are being made.
        locked_user = (
            user.__class__._default_manager.select_for_update().get(pk=user.pk)
        )
        preferences = get_store_preferences(locked_user)
        selected = next((item for item in preferences if item.store == store), None)
        if selected is None:
            raise ValueError("Unknown store")
        locked_user.store_preferences.exclude(pk=selected.pk).update(enabled=False)
        locked_user.store_preferences.filter(pk=selected.pk).update(enabled=True)
        selected.enabled = True
        return selected

import unicodedata
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import migrations, models
from django.utils.text import slugify


TRANSLITERATION = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
    }
)


def ascii_slug(title):
    value = str(title or "").casefold().translate(TRANSLITERATION)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return (slugify(value) or "recipe")[:210].rstrip("-")


def transliterate_existing_slugs(apps, schema_editor):
    Recipe = apps.get_model("recipes", "Recipe")
    reserved = {
        slug
        for slug in Recipe.objects.values_list("slug", flat=True)
        if slug and slug.isascii()
    }
    for recipe in Recipe.objects.order_by("pk"):
        if recipe.slug and recipe.slug.isascii():
            continue
        base = ascii_slug(recipe.title)
        candidate = base
        suffix = 2
        while candidate in reserved:
            candidate = f"{base[: 219 - len(str(suffix))]}-{suffix}"
            suffix += 1
        recipe.slug = candidate
        recipe.save(update_fields=["slug"])
        reserved.add(candidate)


NUTRIENT_FIELDS = (
    ("proteins_per_serving", "белки на порцию, г"),
    ("fats_per_serving", "жиры на порцию, г"),
    ("carbohydrates_per_serving", "углеводы на порцию, г"),
    ("proteins_per_100g", "белки на 100 г"),
    ("fats_per_100g", "жиры на 100 г"),
    ("carbohydrates_per_100g", "углеводы на 100 г"),
)


class Migration(migrations.Migration):
    dependencies = [("recipes", "0009_media_provenance_and_search_indexes")]

    operations = [
        *[
            migrations.AddField(
                model_name="recipe",
                name=name,
                field=models.DecimalField(
                    verbose_name=label,
                    max_digits=8,
                    decimal_places=1,
                    null=True,
                    blank=True,
                    validators=[MinValueValidator(Decimal("0"))],
                ),
            )
            for name, label in NUTRIENT_FIELDS
        ],
        migrations.RunPython(
            transliterate_existing_slugs, migrations.RunPython.noop
        ),
        migrations.AlterField(
            model_name="recipe",
            name="slug",
            field=models.SlugField(blank=True, max_length=220, unique=True),
        ),
    ]

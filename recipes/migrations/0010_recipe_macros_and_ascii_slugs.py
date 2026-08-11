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


ALL_NUTRITION_FIELDS = (
    "calories_per_serving",
    "proteins_per_serving",
    "fats_per_serving",
    "carbohydrates_per_serving",
    "calories_per_100g",
    "proteins_per_100g",
    "fats_per_100g",
    "carbohydrates_per_100g",
)


def prepare_existing_recipes(apps, schema_editor):
    Recipe = apps.get_model("recipes", "Recipe")
    RecipeSlugAlias = apps.get_model("recipes", "RecipeSlugAlias")
    reserved = {
        slug
        for slug in Recipe.objects.values_list("slug", flat=True)
        if slug and slug.isascii()
    }
    for recipe in Recipe.objects.order_by("pk"):
        if not recipe.calories_estimated:
            recipe.nutrition_manual_fields = [
                field
                for field in ALL_NUTRITION_FIELDS
                if getattr(recipe, field) is not None
            ]
            recipe.save(update_fields=["nutrition_manual_fields"])
        if recipe.slug and recipe.slug.isascii():
            continue
        old_slug = recipe.slug
        base = ascii_slug(recipe.title)
        candidate = base
        suffix = 2
        while candidate in reserved:
            candidate = f"{base[: 219 - len(str(suffix))]}-{suffix}"
            suffix += 1
        if old_slug:
            RecipeSlugAlias.objects.create(recipe=recipe, slug=old_slug)
        recipe.slug = candidate
        recipe.save(update_fields=["slug"])
        reserved.add(candidate)


def restore_existing_slugs(apps, schema_editor):
    Recipe = apps.get_model("recipes", "Recipe")
    RecipeSlugAlias = apps.get_model("recipes", "RecipeSlugAlias")
    for recipe in Recipe.objects.order_by("pk"):
        alias = RecipeSlugAlias.objects.filter(recipe=recipe).order_by("pk").first()
        if alias and not Recipe.objects.exclude(pk=recipe.pk).filter(slug=alias.slug).exists():
            recipe.slug = alias.slug
            recipe.save(update_fields=["slug"])


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
        migrations.AddField(
            model_name="recipe",
            name="nutrition_manual_fields",
            field=models.JSONField(default=list, editable=False),
        ),
        migrations.CreateModel(
            name="RecipeSlugAlias",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "slug",
                    models.SlugField(
                        allow_unicode=True, max_length=220, unique=True
                    ),
                ),
                (
                    "recipe",
                    models.ForeignKey(
                        on_delete=models.CASCADE,
                        related_name="slug_aliases",
                        to="recipes.recipe",
                    ),
                ),
            ],
        ),
        migrations.RunPython(
            prepare_existing_recipes, restore_existing_slugs
        ),
        migrations.AlterField(
            model_name="recipe",
            name="slug",
            field=models.SlugField(blank=True, max_length=220, unique=True),
        ),
    ]

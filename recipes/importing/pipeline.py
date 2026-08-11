from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from recipes.categories import CATEGORY_TAXONOMY
from recipes.models import Category, ImportJob, Recipe, RecipeIngredient, RecipeStep

from .exceptions import ImportPipelineError
from .extractors import extract_source
from .llm import adapt_with_ai
from .structured import adapt_structured_recipe


def save_draft(job: ImportJob, data: dict) -> Recipe:
    with transaction.atomic():
        recipe = job.recipe
        if recipe and recipe.status == Recipe.Status.DRAFT:
            recipe.title = data["title"]
            recipe.description = data["description"]
            recipe.servings = data["servings"]
            recipe.prep_minutes = data["prep_minutes"]
            recipe.cook_minutes = data["cook_minutes"]
            recipe.source_url = job.source_url
            recipe.save()
            recipe.ingredients.all().delete()
            recipe.steps.all().delete()
        else:
            recipe = Recipe.objects.create(
                title=data["title"],
                description=data["description"],
                servings=data["servings"],
                prep_minutes=data["prep_minutes"],
                cook_minutes=data["cook_minutes"],
                status=Recipe.Status.DRAFT,
                source_url=job.source_url,
                created_by=job.requested_by,
            )
        RecipeIngredient.objects.bulk_create(
            [
                RecipeIngredient(recipe=recipe, order=index, **ingredient)
                for index, ingredient in enumerate(data["ingredients"])
            ]
        )
        RecipeStep.objects.bulk_create(
            [
                RecipeStep(recipe=recipe, order=index, **step)
                for index, step in enumerate(data["steps"])
            ]
        )
        category_by_slug = {
            category.slug: category
            for category in Category.objects.filter(slug__in=data.get("categories", []))
        }
        taxonomy = dict(CATEGORY_TAXONOMY)
        for index, slug in enumerate(data.get("categories", [])):
            if slug not in category_by_slug and slug in taxonomy:
                category_by_slug[slug], _ = Category.objects.get_or_create(
                    slug=slug,
                    defaults={"name": taxonomy[slug], "order": index},
                )
        recipe.categories.set(
            [category_by_slug[slug] for slug in data.get("categories", []) if slug in category_by_slug]
        )
        job.recipe = recipe
        job.status = ImportJob.Status.COMPLETED
        job.finished_at = timezone.now()
        job.error = ""
        job.save(update_fields=["recipe", "status", "finished_at", "error"])
    return recipe


def process_import_job(job: ImportJob) -> Recipe:
    document = extract_source(job.source_url)
    job.source_title = document.title
    job.save(update_fields=["source_title"])
    if settings.RECIPE_AI_BASE_URL and settings.RECIPE_AI_MODEL:
        data = adapt_with_ai(document)
    elif document.structured_recipe:
        data = adapt_structured_recipe(document.structured_recipe)
    else:
        raise ImportPipelineError(
            "Для этого источника нужен AI-сервис. Страница без Recipe-разметки или YouTube "
            "не могут быть адаптированы автоматически без модели."
        )
    return save_draft(job, data)

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from PIL import Image, UnidentifiedImageError
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from recipes.categories import CATEGORY_TAXONOMY
from recipes.models import Category, ImportJob, Recipe, RecipeIngredient, RecipeStep

from .exceptions import ImportPipelineError
from .extractors import (
    MAX_REDIRECTS,
    USER_AGENT,
    SourceDocument,
    _validate_public_url,
    extract_source,
)
from .llm import adapt_with_ai
from .normalizer import normalize_recipes
from .safety import validate_source_safety
from .structured import adapt_structured_recipe


logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class DownloadedImage:
    name: str
    content: bytes


def _download_image(url: str, *, cover: bool) -> DownloadedImage | None:
    """Fetch and validate a public source image without making image import fatal."""
    current_url = url
    headers = {"User-Agent": USER_AGENT, "Accept": "image/jpeg,image/png,image/webp"}
    try:
        with httpx.Client(
            timeout=15,
            follow_redirects=False,
            headers=headers,
            trust_env=False,
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                _validate_public_url(current_url)
                with client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            return None
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .lower()
                    )
                    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                        return None
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_IMAGE_BYTES:
                            return None
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    with Image.open(io.BytesIO(content)) as image:
                        image.verify()
                    with Image.open(io.BytesIO(content)) as image:
                        width, height = image.size
                    min_area = 150_000 if cover else 40_000
                    min_side = 300 if cover else 160
                    if width * height < min_area or min(width, height) < min_side:
                        return None
                    extension = {
                        "image/jpeg": ".jpg",
                        "image/png": ".png",
                        "image/webp": ".webp",
                    }[content_type]
                    digest = hashlib.sha256(current_url.encode()).hexdigest()[:16]
                    return DownloadedImage(f"import-{digest}{extension}", content)
    except (
        httpx.HTTPError,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        ImportPipelineError,
    ):
        logger.info("Could not import source image %s", url, exc_info=True)
    return None


def _first_downloadable_image(urls: list[str], *, cover: bool) -> DownloadedImage | None:
    for url in dict.fromkeys(urls):
        image = _download_image(url, cover=cover)
        if image:
            return image
    return None


def _prepare_images(
    document: SourceDocument | None,
    recipes: list[dict],
) -> list[tuple[DownloadedImage | None, list[DownloadedImage | None]]]:
    if not document:
        return [(None, [None] * len(data["steps"])) for data in recipes]
    allowed_cover = set(document.cover_image_urls)
    allowed_steps = set(document.step_image_urls)
    structured_cover_urls = {
        url for urls in document.recipe_cover_image_urls for url in urls
    }
    structured_step_urls = {
        url
        for urls in document.recipe_step_image_urls
        for url in urls
        if url
    }
    generic_cover_urls = [
        url for url in document.cover_image_urls if url not in structured_cover_urls
    ]
    generic_step_urls = [
        url for url in document.step_image_urls if url not in structured_step_urls
    ]
    prepared = []
    for recipe_index, data in enumerate(recipes):
        preferred_cover = data.get("cover_image_url", "")
        cover_urls = []
        if preferred_cover in allowed_cover:
            cover_urls.append(preferred_cover)
        if recipe_index < len(document.recipe_cover_image_urls):
            cover_urls.extend(document.recipe_cover_image_urls[recipe_index])
        fallback_covers = (
            document.cover_image_urls if len(recipes) == 1 else generic_cover_urls
        )
        cover_urls.extend(fallback_covers[recipe_index:])
        cover_urls.extend(fallback_covers[:recipe_index])
        cover_image = _first_downloadable_image(cover_urls, cover=True)

        step_images: list[DownloadedImage | None] = []
        fallback_index = 0
        used_step_urls: set[str] = set()
        structured_slots = (
            document.recipe_step_image_urls[recipe_index]
            if recipe_index < len(document.recipe_step_image_urls)
            else ()
        )
        fallback_step_urls = generic_step_urls if structured_slots else document.step_image_urls
        for step_index, step in enumerate(data["steps"]):
            preferred_step = step.get("image_url", "")
            candidates = [preferred_step] if preferred_step in allowed_steps else []
            if not candidates and step_index < len(structured_slots) and structured_slots[step_index]:
                candidates.append(structured_slots[step_index])
            if (
                not candidates
                and len(recipes) == 1
            ):
                while (
                    fallback_index < len(fallback_step_urls)
                    and fallback_step_urls[fallback_index] in used_step_urls
                ):
                    fallback_index += 1
                if fallback_index < len(fallback_step_urls):
                    candidates.append(fallback_step_urls[fallback_index])
                    fallback_index += 1
            used_step_urls.update(candidates)
            step_images.append(_first_downloadable_image(candidates, cover=False))
        prepared.append((cover_image, step_images))
    return prepared


def _recipe_values(job: ImportJob, data: dict) -> dict:
    return {
        "title": data["title"],
        "description": data["description"],
        "servings": data["servings"],
        "prep_minutes": data["prep_minutes"],
        "cook_minutes": data["cook_minutes"],
        "calories_per_serving": data.get("calories_per_serving"),
        "calories_per_100g": data.get("calories_per_100g"),
        "source_url": job.source_url,
    }


def _set_categories(recipe: Recipe, slugs: list[str]) -> None:
    category_by_slug = {
        category.slug: category for category in Category.objects.filter(slug__in=slugs)
    }
    taxonomy = dict(CATEGORY_TAXONOMY)
    for index, slug in enumerate(slugs):
        if slug not in category_by_slug and slug in taxonomy:
            category_by_slug[slug], _ = Category.objects.get_or_create(
                slug=slug,
                defaults={"name": taxonomy[slug], "order": index},
            )
    recipe.categories.set(
        [category_by_slug[slug] for slug in slugs if slug in category_by_slug]
    )


def save_draft(
    job: ImportJob,
    data: dict | list[dict],
    *,
    document: SourceDocument | None = None,
) -> list[Recipe]:
    recipe_data = normalize_recipes(data)
    prepared_images = _prepare_images(document, recipe_data)
    with transaction.atomic():
        existing_drafts: list[Recipe] = []
        if job.recipe and job.recipe.status == Recipe.Status.DRAFT:
            existing_drafts.append(job.recipe)
        existing_drafts.extend(
            job.recipes.filter(status=Recipe.Status.DRAFT)
            .exclude(pk=job.recipe_id)
            .order_by("pk")
        )
        saved_recipes: list[Recipe] = []
        for recipe_index, values in enumerate(recipe_data):
            cover_image, step_images = prepared_images[recipe_index]
            if recipe_index < len(existing_drafts):
                recipe = existing_drafts[recipe_index]
                for field, value in _recipe_values(job, values).items():
                    setattr(recipe, field, value)
                if cover_image:
                    recipe.cover.save(
                        cover_image.name,
                        ContentFile(cover_image.content),
                        save=False,
                    )
                recipe.save()
                recipe.ingredients.all().delete()
                recipe.steps.all().delete()
            else:
                recipe = Recipe(
                    **_recipe_values(job, values),
                    status=Recipe.Status.DRAFT,
                    created_by=job.requested_by,
                )
                if cover_image:
                    recipe.cover.save(
                        cover_image.name,
                        ContentFile(cover_image.content),
                        save=False,
                    )
                recipe.save()

            RecipeIngredient.objects.bulk_create(
                [
                    RecipeIngredient(recipe=recipe, order=index, **ingredient)
                    for index, ingredient in enumerate(values["ingredients"])
                ]
            )
            steps = []
            for index, step in enumerate(values["steps"]):
                step_values = {key: value for key, value in step.items() if key != "image_url"}
                recipe_step = RecipeStep(recipe=recipe, order=index, **step_values)
                step_image = step_images[index]
                if step_image:
                    recipe_step.image.save(
                        step_image.name,
                        ContentFile(step_image.content),
                        save=False,
                    )
                steps.append(recipe_step)
            RecipeStep.objects.bulk_create(steps)
            _set_categories(recipe, values.get("categories", []))
            saved_recipes.append(recipe)

        stale_draft_ids = [recipe.pk for recipe in existing_drafts[len(saved_recipes):]]
        if stale_draft_ids:
            Recipe.objects.filter(pk__in=stale_draft_ids, status=Recipe.Status.DRAFT).delete()
        job.recipe = saved_recipes[0]
        job.recipes.set(saved_recipes)
        job.status = ImportJob.Status.COMPLETED
        job.finished_at = timezone.now()
        job.error = ""
        job.save(update_fields=["recipe", "status", "finished_at", "error"])
    return saved_recipes


def process_import_job(job: ImportJob) -> list[Recipe]:
    document = extract_source(job.source_url)
    validate_source_safety(document)
    job.source_title = document.title
    job.save(update_fields=["source_title"])
    if settings.RECIPE_AI_BASE_URL and settings.RECIPE_AI_MODEL:
        data = adapt_with_ai(document, custom_prompt=job.custom_prompt)
    elif document.all_structured_recipes:
        data = [adapt_structured_recipe(recipe) for recipe in document.all_structured_recipes]
    else:
        raise ImportPipelineError(
            "Для этого источника нужен AI-сервис. Страница без Recipe-разметки или YouTube "
            "не могут быть адаптированы автоматически без модели."
        )
    return save_draft(job, data, document=document)

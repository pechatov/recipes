from __future__ import annotations

import hashlib
import http.client
import io
import logging
import ssl
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urljoin

from PIL import Image, UnidentifiedImageError
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from recipes.categories import CATEGORY_TAXONOMY
from recipes.models import Category, ImportJob, Recipe, RecipeIngredient, RecipeStep

from .exceptions import AIResponseError, ImportPipelineError
from .extractors import (
    MAX_REDIRECTS,
    USER_AGENT,
    SourceDocument,
    _open_public_url,
    extract_source,
)
from .llm import adapt_with_ai
from .normalizer import normalize_recipes
from .safety import validate_source_safety
from .structured import adapt_structured_recipe


logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 40 * 1024 * 1024
MAX_IMPORTED_IMAGES = 25
MAX_IMAGE_WIDTH = 8_000
MAX_IMAGE_HEIGHT = 8_000
MAX_IMAGE_PIXELS = 32_000_000


@dataclass(frozen=True)
class DownloadedImage:
    name: str
    content: bytes
    width: int = 1_000
    height: int = 1_000


def _valid_image_dimensions(image: DownloadedImage, *, cover: bool) -> bool:
    if (
        image.width > MAX_IMAGE_WIDTH
        or image.height > MAX_IMAGE_HEIGHT
        or image.width * image.height > MAX_IMAGE_PIXELS
    ):
        return False
    min_area = 150_000 if cover else 40_000
    min_side = 300 if cover else 160
    return image.width * image.height >= min_area and min(image.width, image.height) >= min_side


@dataclass
class ImageImportBudget:
    cache: dict[str, DownloadedImage | None] = field(default_factory=dict)
    total_bytes: int = 0
    assignments: int = 0

    def select(self, urls: list[str], *, cover: bool) -> DownloadedImage | None:
        if self.assignments >= MAX_IMPORTED_IMAGES:
            return None
        for url in dict.fromkeys(urls):
            if url not in self.cache:
                if len(self.cache) >= MAX_IMPORTED_IMAGES:
                    continue
                image = _download_image(url)
                if image and self.total_bytes + len(image.content) <= MAX_IMAGE_TOTAL_BYTES:
                    self.total_bytes += len(image.content)
                    self.cache[url] = image
                else:
                    self.cache[url] = None
            image = self.cache[url]
            if image and _valid_image_dimensions(image, cover=cover):
                self.assignments += 1
                return image
        return None


def _download_image(url: str) -> DownloadedImage | None:
    """Fetch and validate a public source image without making image import fatal."""
    current_url = url
    headers = {"User-Agent": USER_AGENT, "Accept": "image/jpeg,image/png,image/webp"}
    try:
        for _ in range(MAX_REDIRECTS + 1):
            with _open_public_url(current_url, headers=headers, timeout=15) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current_url = urljoin(current_url, location)
                    continue
                if response.status >= 400:
                    return None
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .lower()
                )
                if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                    return None
                chunks: list[bytes] = []
                size = 0
                while chunk := response.read(min(65_536, MAX_IMAGE_BYTES + 1 - size)):
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        return None
                    chunks.append(chunk)
                content = b"".join(chunks)
                with Image.open(io.BytesIO(content)) as image:
                    width, height = image.size
                    if (
                        width > MAX_IMAGE_WIDTH
                        or height > MAX_IMAGE_HEIGHT
                        or width * height > MAX_IMAGE_PIXELS
                    ):
                        return None
                    image.verify()
                if width < 1 or height < 1:
                    return None
                extension = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/webp": ".webp",
                }[content_type]
                digest = hashlib.sha256(current_url.encode()).hexdigest()[:16]
                return DownloadedImage(
                    f"import-{digest}{extension}",
                    content,
                    width,
                    height,
                )
    except (
        http.client.HTTPException,
        ssl.SSLError,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        ImportPipelineError,
    ):
        logger.info("Could not import source image %s", url, exc_info=True)
    return None


def _prepare_images(
    document: SourceDocument | None,
    recipes: list[dict],
) -> list[tuple[DownloadedImage | None, list[DownloadedImage | None]]]:
    if not document:
        return [(None, [None] * len(data["steps"])) for data in recipes]
    budget = ImageImportBudget()
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
        cover_image = budget.select(cover_urls, cover=True)

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
            step_images.append(budget.select(candidates, cover=False))
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
        "proteins_per_serving": data.get("proteins_per_serving"),
        "fats_per_serving": data.get("fats_per_serving"),
        "carbohydrates_per_serving": data.get("carbohydrates_per_serving"),
        "proteins_per_100g": data.get("proteins_per_100g"),
        "fats_per_100g": data.get("fats_per_100g"),
        "carbohydrates_per_100g": data.get("carbohydrates_per_100g"),
        "calories_estimated": True,
        "nutrition_manual_fields": [],
        "source_url": job.source_url,
    }


def _stored_file(field_file) -> tuple[Any, str] | None:
    name = str(field_file.name or "")
    return (field_file.storage, name) if name else None


def _delete_stored_files(files: list[tuple[Any, str]]) -> None:
    seen = set()
    for storage, name in files:
        key = (id(storage), name)
        if key in seen:
            continue
        seen.add(key)
        try:
            storage.delete(name)
        except Exception:
            logger.warning("Could not delete obsolete imported image %s", name, exc_info=True)


def _step_stored_files(recipe: Recipe) -> list[tuple[Any, str]]:
    return [
        stored
        for step in recipe.steps.filter(image_imported=True).exclude(image="").only("image")
        if (stored := _stored_file(step.image))
    ]


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


def _adapt_structured_recipes(
    document: SourceDocument,
) -> tuple[list[dict], SourceDocument]:
    adapted = []
    valid_indices = []
    structured_recipes = document.all_structured_recipes
    for index, recipe in enumerate(structured_recipes):
        try:
            adapted.append(adapt_structured_recipe(recipe))
            valid_indices.append(index)
        except (AIResponseError, TypeError, ValueError, ZeroDivisionError):
            logger.info("Skipping incomplete JSON-LD Recipe at index %s", index)
    if not adapted:
        raise ImportPipelineError(
            "На странице найдена только неполная Recipe-разметка: в ней нет "
            "названия, ингредиентов или шагов приготовления."
        )

    def matching_slots(values: tuple[tuple[str, ...], ...]) -> tuple[tuple[str, ...], ...]:
        return tuple(values[index] for index in valid_indices if index < len(values))

    filtered_document = replace(
        document,
        structured_recipe=None,
        structured_recipes=tuple(structured_recipes[index] for index in valid_indices),
        recipe_cover_image_urls=matching_slots(document.recipe_cover_image_urls),
        recipe_step_image_urls=matching_slots(document.recipe_step_image_urls),
    )
    return adapted, filtered_document


def save_draft(
    job: ImportJob,
    data: dict | list[dict],
    *,
    document: SourceDocument | None = None,
    expected_draft_versions: dict[int, Any] | None = None,
) -> list[Recipe]:
    if expected_draft_versions is None:
        expected_draft_versions = {
            recipe.pk: recipe.updated_at
            for recipe in Recipe.objects.filter(
                Q(import_jobs=job) | Q(import_job=job),
                status=Recipe.Status.DRAFT,
            ).distinct()
        }
    recipe_data = normalize_recipes(data)
    prepared_images = _prepare_images(document, recipe_data)
    new_files: list[tuple[Any, str]] = []
    old_files: list[tuple[Any, str]] = []
    try:
        with transaction.atomic():
            locked_job = ImportJob.objects.select_for_update().get(pk=job.pk)
            linked_recipe_ids = set(locked_job.recipes.values_list("pk", flat=True))
            if locked_job.recipe_id:
                linked_recipe_ids.add(locked_job.recipe_id)
            locked_recipes = {
                recipe.pk: recipe
                for recipe in Recipe.objects.select_for_update()
                .filter(pk__in=linked_recipe_ids)
                .order_by("pk")
            }
            if any(
                recipe.status == Recipe.Status.PUBLISHED
                for recipe in locked_recipes.values()
            ):
                raise ImportPipelineError(
                    "Нельзя повторно обработать импорт после публикации одного из "
                    "рецептов. Оставшиеся черновики сохранены без изменений."
                )
            current_versions = {
                recipe.pk: recipe.updated_at for recipe in locked_recipes.values()
            }
            if current_versions != expected_draft_versions:
                raise ImportPipelineError(
                    "Черновики изменились во время обработки. Повторный импорт отменён, "
                    "чтобы не потерять пользовательские правки."
                )
            if RecipeStep.objects.filter(
                recipe_id__in=linked_recipe_ids,
                image_imported=False,
            ).exclude(image="").exists():
                raise ImportPipelineError(
                    "В черновиках есть фотографии шагов, добавленные вручную. "
                    "Повторный импорт отменён, чтобы не удалить их."
                )
            existing_drafts: list[Recipe] = []
            primary_recipe = locked_recipes.get(locked_job.recipe_id)
            if primary_recipe:
                existing_drafts.append(primary_recipe)
            existing_drafts.extend(
                recipe
                for recipe_id, recipe in locked_recipes.items()
                if recipe_id != locked_job.recipe_id
            )
            saved_recipes: list[Recipe] = []
            for recipe_index, values in enumerate(recipe_data):
                cover_image, step_images = prepared_images[recipe_index]
                if recipe_index < len(existing_drafts):
                    recipe = existing_drafts[recipe_index]
                    for field, value in _recipe_values(locked_job, values).items():
                        setattr(recipe, field, value)
                    if cover_image and (not recipe.cover or recipe.cover_imported):
                        if recipe.cover_imported:
                            old_cover = _stored_file(recipe.cover)
                            if old_cover:
                                old_files.append(old_cover)
                        recipe.cover.save(
                            cover_image.name,
                            ContentFile(cover_image.content),
                            save=False,
                        )
                        new_cover = _stored_file(recipe.cover)
                        if new_cover:
                            new_files.append(new_cover)
                        recipe.cover_imported = True
                    old_files.extend(_step_stored_files(recipe))
                    recipe.save()
                    recipe.ingredients.all().delete()
                    recipe.steps.all().delete()
                else:
                    recipe = Recipe(
                        **_recipe_values(locked_job, values),
                        status=Recipe.Status.DRAFT,
                        created_by=locked_job.requested_by,
                    )
                    if cover_image:
                        recipe.cover.save(
                            cover_image.name,
                            ContentFile(cover_image.content),
                            save=False,
                        )
                        new_cover = _stored_file(recipe.cover)
                        if new_cover:
                            new_files.append(new_cover)
                        recipe.cover_imported = True
                    recipe.save()

                RecipeIngredient.objects.bulk_create(
                    [
                        RecipeIngredient(recipe=recipe, order=index, **ingredient)
                        for index, ingredient in enumerate(values["ingredients"])
                    ]
                )
                steps = []
                for index, step in enumerate(values["steps"]):
                    step_values = {
                        key: value for key, value in step.items() if key != "image_url"
                    }
                    recipe_step = RecipeStep(recipe=recipe, order=index, **step_values)
                    step_image = step_images[index]
                    if step_image:
                        recipe_step.image.save(
                            step_image.name,
                            ContentFile(step_image.content),
                            save=False,
                        )
                        new_step = _stored_file(recipe_step.image)
                        if new_step:
                            new_files.append(new_step)
                        recipe_step.image_imported = True
                    steps.append(recipe_step)
                RecipeStep.objects.bulk_create(steps)
                _set_categories(recipe, values.get("categories", []))
                saved_recipes.append(recipe)

            locked_job.recipe = saved_recipes[0]
            # A non-deterministic re-import may return fewer recipes. Detach
            # unmatched drafts from this job instead of deleting user edits.
            locked_job.recipes.set(saved_recipes)
            locked_job.status = ImportJob.Status.COMPLETED
            locked_job.finished_at = timezone.now()
            locked_job.error = ""
            locked_job.save(update_fields=["recipe", "status", "finished_at", "error"])

            kept_file_refs = []
            for recipe in saved_recipes:
                cover_ref = _stored_file(recipe.cover)
                if cover_ref:
                    kept_file_refs.append(cover_ref)
                kept_file_refs.extend(_step_stored_files(recipe))
            kept_files = {
                (id(storage), name) for storage, name in kept_file_refs
            }
            obsolete_files = [
                (storage, name)
                for storage, name in old_files
                if (id(storage), name) not in kept_files
            ]
            transaction.on_commit(
                lambda files=obsolete_files: _delete_stored_files(files)
            )
    except Exception:
        _delete_stored_files(new_files)
        raise
    job.recipe = locked_job.recipe
    job.status = locked_job.status
    job.finished_at = locked_job.finished_at
    job.error = locked_job.error
    return saved_recipes


def process_import_job(job: ImportJob) -> list[Recipe]:
    expected_draft_versions = {
        recipe.pk: recipe.updated_at
        for recipe in Recipe.objects.filter(
            Q(import_jobs=job) | Q(import_job=job),
            status=Recipe.Status.DRAFT,
        ).distinct()
    }
    document = extract_source(job.source_url)
    validate_source_safety(document)
    job.source_title = document.title
    job.save(update_fields=["source_title"])
    if settings.RECIPE_AI_BASE_URL and settings.RECIPE_AI_MODEL:
        data = adapt_with_ai(document, custom_prompt=job.custom_prompt)
    elif document.all_structured_recipes:
        data, document = _adapt_structured_recipes(document)
    else:
        raise ImportPipelineError(
            "Для этого источника нужен AI-сервис. Страница без Recipe-разметки или YouTube "
            "не могут быть адаптированы автоматически без модели."
        )
    return save_draft(
        job,
        data,
        document=document,
        expected_draft_versions=expected_draft_versions,
    )

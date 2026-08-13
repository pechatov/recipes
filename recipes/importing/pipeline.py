from __future__ import annotations

import hashlib
import http.client
import io
import json
import logging
import ssl
import time
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlencode, urljoin

from PIL import Image, UnidentifiedImageError
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from recipes.categories import CATEGORY_TAXONOMY
from recipes.models import (
    Category,
    ImportJob,
    Recipe,
    RecipeIngredient,
    RecipeRefinement,
    RecipeStep,
)

from .exceptions import AIResponseError, ImportPipelineError
from .extractors import (
    MAX_REDIRECTS,
    USER_AGENT,
    SourceDocument,
    _open_public_url,
    extract_source,
    fetch_source_title,
    youtube_video_id,
)
from .llm import adapt_with_ai, refine_with_ai
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
MIN_COVER_SHORT_SIDE = 800
MIN_COVER_LONG_SIDE = 1_200
MAX_IMAGE_SEARCH_BYTES = 512 * 1024
MAX_COVER_SEARCH_QUERIES = 2
MAX_COVER_SEARCH_SECONDS = 6
OPENVERSE_REQUEST_TIMEOUT_SECONDS = 3
OPENVERSE_SEARCH_URL = "https://api.openverse.org/v1/images/"
CATEGORY_IMAGE_QUERIES = {
    "breakfast": "breakfast food",
    "appetizer": "appetizer food",
    "soup": "soup bowl",
    "salad": "salad bowl",
    "main-course": "cooked main dish",
    "side-dish": "side dish",
    "bakery": "baked goods",
    "dessert": "dessert",
    "drink": "drink",
    "sauce": "sauce food",
    "preserve": "fruit preserves",
    "other": "cooked food",
}


@dataclass(frozen=True)
class DownloadedImage:
    name: str
    content: bytes
    width: int = 1_600
    height: int = 1_200


def _valid_image_dimensions(image: DownloadedImage, *, cover: bool) -> bool:
    if (
        image.width > MAX_IMAGE_WIDTH
        or image.height > MAX_IMAGE_HEIGHT
        or image.width * image.height > MAX_IMAGE_PIXELS
    ):
        return False
    if cover:
        return (
            min(image.width, image.height) >= MIN_COVER_SHORT_SIDE
            and max(image.width, image.height) >= MIN_COVER_LONG_SIDE
        )
    return image.width * image.height >= 40_000 and min(image.width, image.height) >= 160


@dataclass
class ImageImportBudget:
    cache: dict[str, DownloadedImage | None] = field(default_factory=dict)
    attempted_urls: set[str] = field(default_factory=set)
    total_bytes: int = 0
    assignments: int = 0

    @property
    def exhausted(self) -> bool:
        return (
            self.assignments >= MAX_IMPORTED_IMAGES
            or len(self.attempted_urls) >= MAX_IMPORTED_IMAGES
            or self.total_bytes >= MAX_IMAGE_TOTAL_BYTES
        )

    def select(
        self,
        urls: list[str],
        *,
        cover: bool,
        deadline: float | None = None,
    ) -> DownloadedImage | None:
        if self.exhausted:
            return None
        for url in dict.fromkeys(urls):
            if deadline is not None and time.monotonic() >= deadline:
                break
            if url not in self.cache:
                if len(self.attempted_urls) >= MAX_IMPORTED_IMAGES:
                    continue
                self.attempted_urls.add(url)
                if deadline is None:
                    image = _download_image(url)
                else:
                    image = _download_image(
                        url,
                        timeout=max(0.001, deadline - time.monotonic()),
                        deadline=deadline,
                    )
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


def _download_image(
    url: str,
    *,
    timeout: float = 15,
    deadline: float | None = None,
) -> DownloadedImage | None:
    """Fetch and validate a public source image without making image import fatal."""
    current_url = url
    # Some image proxy endpoints reject a narrow Accept header even when they
    # return JPEG. The response is still restricted by content type and Pillow.
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    try:
        for _ in range(MAX_REDIRECTS + 1):
            request_timeout = timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                request_timeout = min(request_timeout, remaining)
            with _open_public_url(
                current_url,
                headers=headers,
                timeout=max(0.001, request_timeout),
            ) as response:
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
                while True:
                    if deadline is not None and time.monotonic() >= deadline:
                        return None
                    chunk = response.read(min(65_536, MAX_IMAGE_BYTES + 1 - size))
                    if not chunk:
                        break
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


def _search_cover_image_urls(
    query: str,
    *,
    timeout: float = OPENVERSE_REQUEST_TIMEOUT_SECONDS,
) -> list[str]:
    """Find relevant public-domain cover candidates without making import fatal."""
    query = " ".join(str(query or "").split())[:200]
    if not query:
        return []
    params = urlencode(
        {
            "q": query,
            "page_size": 5,
            "license": "cc0,pdm",
            "mature": "false",
            "size": "large",
        }
    )
    url = f"{OPENVERSE_SEARCH_URL}?{params}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        with _open_public_url(url, headers=headers, timeout=timeout) as response:
            if response.status != 200:
                return []
            content_type = (
                response.headers.get("content-type", "")
                .split(";", 1)[0]
                .lower()
            )
            if content_type != "application/json":
                return []
            content = response.read(MAX_IMAGE_SEARCH_BYTES + 1)
            if len(content) > MAX_IMAGE_SEARCH_BYTES:
                return []
        payload = json.loads(content)
        results = payload.get("results", []) if isinstance(payload, dict) else []
        candidates = []
        for result in results if isinstance(results, list) else []:
            if (
                not isinstance(result, dict)
                or result.get("license") not in {"cc0", "pdm"}
                or result.get("mature") is True
            ):
                continue
            for candidate in (result.get("thumbnail"), result.get("url")):
                if isinstance(candidate, str) and candidate.startswith(
                    ("http://", "https://")
                ):
                    candidates.append(candidate[:2048])
        return list(dict.fromkeys(candidates))
    except (
        json.JSONDecodeError,
        http.client.HTTPException,
        ssl.SSLError,
        OSError,
        ImportPipelineError,
    ):
        logger.info("Could not search for a cover image using %r", query, exc_info=True)
        return []


def _cover_search_queries(data: dict) -> list[str]:
    precise_query = " ".join(
        str(data.get("cover_image_search_query", "") or "").split()
    )
    category_query = next(
        (
            CATEGORY_IMAGE_QUERIES[slug]
            for slug in data.get("categories", [])
            if slug in CATEGORY_IMAGE_QUERIES
        ),
        "",
    )
    if precise_query:
        precise_words = precise_query.split()
        fallback_query = precise_words[0] if len(precise_words) > 1 else category_query
        queries = [precise_query, fallback_query]
    else:
        queries = [data.get("title", ""), category_query]
    return list(
        dict.fromkeys(
            " ".join(str(query or "").split()) for query in queries if query
        )
    )[:MAX_COVER_SEARCH_QUERIES]


def _prepare_images(
    document: SourceDocument | None,
    recipes: list[dict],
) -> list[tuple[DownloadedImage | None, list[DownloadedImage | None]]]:
    if not document:
        return [(None, [None] * len(data["steps"])) for data in recipes]
    budget = ImageImportBudget()
    search_cache: dict[str, list[str]] = {}
    search_seconds_remaining = MAX_COVER_SEARCH_SECONDS
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
        if not cover_image and data.get("title") and not budget.exhausted:
            search_attempts = 0
            recipe_search_started_at = time.monotonic()
            remaining_recipes = len(recipes) - recipe_index
            recipe_search_deadline = recipe_search_started_at + (
                search_seconds_remaining / max(remaining_recipes, 1)
            )
            try:
                for query in _cover_search_queries(data):
                    if budget.exhausted:
                        break
                    if query not in search_cache:
                        remaining = recipe_search_deadline - time.monotonic()
                        if (
                            search_attempts >= MAX_COVER_SEARCH_QUERIES
                            or remaining <= 0
                        ):
                            break
                        search_attempts += 1
                        search_cache[query] = list(
                            _search_cover_image_urls(
                                query,
                                timeout=min(
                                    OPENVERSE_REQUEST_TIMEOUT_SECONDS,
                                    remaining,
                                ),
                            )
                            or []
                        )
                    search_urls = list(search_cache[query])
                    if search_urls:
                        offset = recipe_index % len(search_urls)
                        search_urls = search_urls[offset:] + search_urls[:offset]
                    cover_image = budget.select(
                        search_urls,
                        cover=True,
                        deadline=recipe_search_deadline,
                    )
                    if cover_image:
                        break
            finally:
                search_seconds_remaining = max(
                    0,
                    search_seconds_remaining
                    - max(0, time.monotonic() - recipe_search_started_at),
                )

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


def _recipe_content_values(data: dict) -> dict:
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
    }


def _recipe_values(job: ImportJob, data: dict) -> dict:
    return {
        **_recipe_content_values(data),
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
    youtube_title_attempted = False
    if job.source_type == ImportJob.SourceType.YOUTUBE and not job.source_title:
        youtube_title_attempted = True
        job.source_title = fetch_source_title(job.source_url)
        if job.source_title:
            job.save(update_fields=["source_title"])
    document = extract_source(
        job.source_url,
        source_title=job.source_title,
        fetch_title=not youtube_title_attempted,
    )
    technical_youtube_title = f"YouTube {youtube_video_id(job.source_url) or ''}".strip()
    if job.source_title and document.title == technical_youtube_title:
        document = replace(document, title=job.source_title)
    validate_source_safety(document)
    if document.title and job.source_title != document.title:
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


def _recipe_refinement_payload(recipe: Recipe) -> dict[str, Any]:
    def json_number(value):
        return str(value) if value is not None else None

    return {
        "title": recipe.title,
        "description": recipe.description,
        "servings": recipe.servings,
        "prep_minutes": recipe.prep_minutes,
        "cook_minutes": recipe.cook_minutes,
        "calories_per_serving": json_number(recipe.calories_per_serving),
        "proteins_per_serving": json_number(recipe.proteins_per_serving),
        "fats_per_serving": json_number(recipe.fats_per_serving),
        "carbohydrates_per_serving": json_number(recipe.carbohydrates_per_serving),
        "calories_per_100g": json_number(recipe.calories_per_100g),
        "proteins_per_100g": json_number(recipe.proteins_per_100g),
        "fats_per_100g": json_number(recipe.fats_per_100g),
        "carbohydrates_per_100g": json_number(recipe.carbohydrates_per_100g),
        "categories": list(recipe.categories.values_list("slug", flat=True)),
        "cover_image_url": "",
        "cover_image_search_query": "",
        "ingredients": [
            {
                "section": ingredient.section,
                "name": ingredient.name,
                "quantity": json_number(ingredient.quantity),
                "unit": ingredient.unit,
                "search_query": ingredient.search_query,
                "optional": ingredient.optional,
                "estimated": ingredient.estimated,
                "is_pantry": ingredient.is_pantry,
            }
            for ingredient in recipe.ingredients.all()
        ],
        "steps": [
            {
                "section": step.section,
                "title": step.title,
                "instruction": step.instruction,
                "image_url": "",
            }
            for step in recipe.steps.all()
        ],
    }


def save_refined_recipe(
    refinement: RecipeRefinement,
    data: dict,
) -> Recipe:
    values = normalize_recipes(
        {"recipes": [data]},
        require_quantities=True,
        keep_ingredient_notes=False,
        require_categories=True,
    )[0]
    with transaction.atomic():
        locked_refinement = RecipeRefinement.objects.select_for_update().get(
            pk=refinement.pk
        )
        recipe = (
            Recipe.objects.select_for_update()
            .prefetch_related("ingredients", "steps", "categories")
            .get(pk=locked_refinement.recipe_id)
        )
        if recipe.status != Recipe.Status.DRAFT:
            raise ImportPipelineError(
                "Рецепт уже опубликован. Пожелание не применено к готовому рецепту."
            )
        if recipe.updated_at != locked_refinement.expected_recipe_updated_at:
            raise ImportPipelineError(
                "Черновик изменился во время обработки. Пожелание не применено, "
                "чтобы не потерять новые правки."
            )
        if recipe.steps.filter(image_imported=False).exclude(image="").exists():
            raise ImportPipelineError(
                "В черновике есть фотографии шагов, добавленные вручную. "
                "Пожелание не применено, чтобы не привязать их к другим шагам."
            )

        previous_steps = list(recipe.steps.all())
        previous_images = [
            (step.image.name, step.image_imported) if step.image else ("", False)
            for step in previous_steps
        ]
        obsolete_images = [
            stored
            for step in previous_steps[len(values["steps"]) :]
            if step.image_imported and (stored := _stored_file(step.image))
        ]

        for field, value in _recipe_content_values(values).items():
            setattr(recipe, field, value)
        recipe.save()
        recipe.ingredients.all().delete()
        recipe.steps.all().delete()
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
            if index < len(previous_images):
                image_name, image_imported = previous_images[index]
                recipe_step.image.name = image_name
                recipe_step.image_imported = image_imported
            steps.append(recipe_step)
        RecipeStep.objects.bulk_create(steps)
        _set_categories(recipe, values.get("categories", []))

        locked_refinement.status = RecipeRefinement.Status.COMPLETED
        locked_refinement.error = ""
        locked_refinement.finished_at = timezone.now()
        locked_refinement.save(update_fields=["status", "error", "finished_at"])
        transaction.on_commit(
            lambda files=obsolete_images: _delete_stored_files(files)
        )

    refinement.status = locked_refinement.status
    refinement.error = locked_refinement.error
    refinement.finished_at = locked_refinement.finished_at
    return recipe


def process_recipe_refinement(refinement: RecipeRefinement) -> Recipe:
    recipe = (
        Recipe.objects.prefetch_related("ingredients", "steps", "categories")
        .get(pk=refinement.recipe_id)
    )
    if recipe.status != Recipe.Status.DRAFT:
        raise ImportPipelineError("Перерабатывать через чат можно только черновики.")
    if recipe.updated_at != refinement.expected_recipe_updated_at:
        raise ImportPipelineError(
            "Черновик изменился до начала обработки. Пожелание не отправлено "
            "Гермесу, чтобы не расходовать ресурсы на устаревшую версию."
        )
    if recipe.steps.filter(image_imported=False).exclude(image="").exists():
        raise ImportPipelineError(
            "В черновике есть фотографии шагов, добавленные вручную. "
            "Пожелание не применено, чтобы не привязать их к другим шагам."
        )
    data = refine_with_ai(_recipe_refinement_payload(recipe), refinement.prompt)
    return save_refined_recipe(refinement, data)

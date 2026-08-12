from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import YouTubeTranscriptApiException

from .exceptions import SourceError


MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_CHARS = 60_000
MAX_TITLE_BYTES = 256 * 1024
MAX_REDIRECTS = 3
USER_AGENT = "FamilyRecipesImporter/1.0"


@dataclass(frozen=True)
class SourceDocument:
    source_type: str
    title: str
    text: str
    structured_recipe: dict[str, Any] | None = None
    structured_recipes: tuple[dict[str, Any], ...] = ()
    cover_image_urls: tuple[str, ...] = ()
    step_image_urls: tuple[str, ...] = ()
    recipe_cover_image_urls: tuple[tuple[str, ...], ...] = ()
    recipe_step_image_urls: tuple[tuple[str, ...], ...] = ()

    @property
    def all_structured_recipes(self) -> tuple[dict[str, Any], ...]:
        """Return every JSON-LD recipe while retaining the legacy singular field."""
        recipes = list(self.structured_recipes)
        if self.structured_recipe and self.structured_recipe not in recipes:
            recipes.insert(0, self.structured_recipe)
        return tuple(recipes)


def youtube_video_id(url: str) -> str | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            candidate = parts[1]
        else:
            candidate = ""
    else:
        return None
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None


def detect_source_type(url: str) -> str:
    return "youtube" if youtube_video_id(url) else "website"


def _resolve_public_url(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SourceError("Нужна публичная ссылка с адресом http:// или https://.")
    if parsed.username or parsed.password:
        raise SourceError("Ссылки с логином или паролем не поддерживаются.")
    try:
        port = parsed.port
    except ValueError as error:
        raise SourceError("В ссылке указан некорректный порт.") from error
    if port not in {None, 80, 443}:
        raise SourceError("Ссылки на нестандартные сетевые порты не поддерживаются.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise SourceError("Не удалось найти сайт по указанному адресу.") from error
    if not addresses:
        raise SourceError("Не удалось найти сайт по указанному адресу.")
    public_ips = []
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise SourceError("Импорт из локальной или служебной сети запрещён.")
        public_ips.append(ip)
    # Many hosts publish IPv6 first even on machines without a working IPv6
    # route. Prefer the validated IPv4 address and retain IPv6-only support.
    pinned_ip = next((ip for ip in public_ips if ip.version == 4), public_ips[0])
    return parsed, str(pinned_ip)


def _validate_public_url(url: str) -> None:
    _resolve_public_url(url)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, pinned_ip, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(host, port, **kwargs)

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, pinned_ip, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(host, port, **kwargs)

    def connect(self):
        raw_socket = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


@contextmanager
def _open_public_url(url: str, *, headers: dict[str, str], timeout: float):
    parsed, pinned_ip = _resolve_public_url(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        connection = _PinnedHTTPSConnection(
            parsed.hostname,
            port,
            pinned_ip,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        connection = _PinnedHTTPConnection(
            parsed.hostname,
            port,
            pinned_ip,
            timeout=timeout,
        )
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    try:
        connection.request("GET", target, headers=headers)
        yield connection.getresponse()
    finally:
        connection.close()


def _download_html(url: str) -> tuple[str, str]:
    current_url = url
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    for _ in range(MAX_REDIRECTS + 1):
        try:
            with _open_public_url(current_url, headers=headers, timeout=20) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise SourceError("Сайт вернул перенаправление без нового адреса.")
                    current_url = urljoin(current_url, location)
                    continue
                if response.status >= 400:
                    raise SourceError(f"Сайт вернул ошибку HTTP {response.status}.")
                content_type = response.headers.get("content-type", "").lower()
                if "html" not in content_type:
                    raise SourceError("По ссылке не найдена HTML-страница с рецептом.")
                chunks: list[bytes] = []
                size = 0
                while chunk := response.read(min(65_536, MAX_SOURCE_BYTES + 1 - size)):
                    size += len(chunk)
                    if size > MAX_SOURCE_BYTES:
                        raise SourceError("Страница слишком большая для безопасного импорта.")
                    chunks.append(chunk)
                encoding = response.headers.get_content_charset() or "utf-8"
                return b"".join(chunks).decode(encoding, errors="replace"), current_url
        except SourceError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError) as error:
            raise SourceError("Не удалось загрузить страницу с рецептом.") from error
    raise SourceError("Сайт перенаправляет запрос слишком много раз.")


def _page_title(soup: BeautifulSoup) -> str:
    heading = soup.find("h1")
    if heading:
        title = heading.get_text(" ", strip=True)
        if title:
            return title[:300]
    for selector in ('meta[property="og:title"]', 'meta[name="twitter:title"]'):
        element = soup.select_one(selector)
        title = str(element.get("content") or "").strip() if element else ""
        if title:
            return title[:300]
    if soup.title:
        title = soup.title.get_text(" ", strip=True)
        if title:
            return title[:300]
    return ""


def _fetch_website_title(url: str) -> str:
    """Fetch only enough of a page to name a queued import without blocking it for long."""
    current_url = url
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    for _ in range(MAX_REDIRECTS + 1):
        try:
            with _open_public_url(current_url, headers=headers, timeout=5) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        return ""
                    current_url = urljoin(current_url, location)
                    continue
                if response.status >= 400:
                    return ""
                if "html" not in response.headers.get("content-type", "").lower():
                    return ""
                content = response.read(MAX_TITLE_BYTES)
                encoding = response.headers.get_content_charset() or "utf-8"
                return _page_title(
                    BeautifulSoup(content.decode(encoding, errors="replace"), "html.parser")
                )
        except (OSError, http.client.HTTPException, ssl.SSLError, SourceError):
            return ""
    return ""


def _fetch_youtube_title(video_id: str) -> str:
    params = urlencode(
        {
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "format": "json",
        }
    )
    url = f"https://www.youtube.com/oembed?{params}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        with _open_public_url(url, headers=headers, timeout=5) as response:
            if response.status != 200:
                return ""
            if "json" not in response.headers.get("content-type", "").lower():
                return ""
            content = response.read(MAX_TITLE_BYTES + 1)
            if len(content) > MAX_TITLE_BYTES:
                return ""
        payload = json.loads(content)
        title = payload.get("title", "") if isinstance(payload, dict) else ""
        return " ".join(str(title).split())[:300]
    except (
        json.JSONDecodeError,
        OSError,
        http.client.HTTPException,
        ssl.SSLError,
        SourceError,
    ):
        return ""


def fetch_source_title(url: str) -> str:
    """Return a best-effort video or article title for task-list presentation."""
    video_id = youtube_video_id(url)
    if video_id:
        return _fetch_youtube_title(video_id)
    return _fetch_website_title(url)


def _find_recipe_jsons(value: Any) -> list[dict[str, Any]]:
    recipes: list[dict[str, Any]] = []
    if isinstance(value, dict):
        recipe_type = value.get("@type")
        types = recipe_type if isinstance(recipe_type, list) else [recipe_type]
        if any(str(item).lower() == "recipe" for item in types):
            recipes.append(value)
            return recipes
        for nested in value.values():
            recipes.extend(_find_recipe_jsons(nested))
    elif isinstance(value, list):
        for nested in value:
            recipes.extend(_find_recipe_jsons(nested))
    return recipes


def _find_recipe_json(value: Any) -> dict[str, Any] | None:
    recipes = _find_recipe_jsons(value)
    return recipes[0] if recipes else None


def _structured_recipes(soup: BeautifulSoup) -> list[dict[str, Any]]:
    recipes: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for recipe in _find_recipe_jsons(value):
            if recipe not in recipes:
                recipes.append(recipe)
    return recipes


def _structured_recipe(soup: BeautifulSoup) -> dict[str, Any] | None:
    recipes = _structured_recipes(soup)
    return recipes[0] if recipes else None


def _absolute_image_url(value: Any, base_url: str) -> str | None:
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl")
    value = str(value or "").strip()
    if not value or value.startswith("data:"):
        return None
    candidate = urljoin(base_url, value)
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return candidate[:2048]


def _schema_image_urls(value: Any, base_url: str) -> list[str]:
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_schema_image_urls(item, base_url))
        return result
    url = _absolute_image_url(value, base_url)
    return [url] if url else []


def _source_images(
    soup: BeautifulSoup,
    recipes: list[dict[str, Any]],
    base_url: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    cover_candidates: list[str] = []
    step_candidates: list[str] = []

    def append_instruction_images(instructions: Any) -> None:
        if not isinstance(instructions, list):
            instructions = [instructions]
        for instruction in instructions:
            if not isinstance(instruction, dict):
                continue
            step_candidates.extend(_schema_image_urls(instruction.get("image"), base_url))
            append_instruction_images(instruction.get("itemListElement", []))

    for recipe in recipes:
        cover_candidates.extend(_schema_image_urls(recipe.get("image"), base_url))
        append_instruction_images(recipe.get("recipeInstructions", []))

    for selector, attribute in (
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('link[rel="image_src"]', "href"),
    ):
        element = soup.select_one(selector)
        if element:
            url = _absolute_image_url(element.get(attribute), base_url)
            if url:
                cover_candidates.append(url)

    scored_images: list[tuple[int, str]] = []
    for image in soup.select("main img, article img"):
        raw_url = (
            image.get("src")
            or image.get("data-src")
            or image.get("data-lazy-src")
            or image.get("data-original")
        )
        url = _absolute_image_url(raw_url, base_url)
        if not url:
            continue
        try:
            width = int(str(image.get("width") or "0").rstrip("px"))
            height = int(str(image.get("height") or "0").rstrip("px"))
        except ValueError:
            width = height = 0
        scored_images.append((width * height, url))
    for _, url in sorted(scored_images, reverse=True):
        cover_candidates.append(url)
        step_candidates.append(url)

    def unique(values: list[str], limit: int) -> tuple[str, ...]:
        return tuple(dict.fromkeys(values))[:limit]

    return unique(cover_candidates, 12), unique(step_candidates, 30)


def _structured_step_image_slots(value: Any, base_url: str) -> tuple[str, ...]:
    """Keep an image slot for each structured step so positions are not lost."""
    values = value if isinstance(value, list) else [value]
    slots: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            slots.append("")
            continue
        item_type = str(item.get("@type") or "").lower()
        if item_type == "howtosection":
            slots.extend(
                _structured_step_image_slots(item.get("itemListElement", []), base_url)
            )
            continue
        urls = _schema_image_urls(item.get("image"), base_url)
        slots.append(urls[0] if urls else "")
    return tuple(slots)


def extract_website(url: str) -> SourceDocument:
    html, final_url = _download_html(url)
    soup = BeautifulSoup(html, "html.parser")
    recipes = _structured_recipes(soup)
    cover_image_urls, step_image_urls = _source_images(soup, recipes, final_url)
    for element in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        element.decompose()
    title = _page_title(soup)
    text = "\n".join(
        line.strip()
        for line in soup.get_text("\n").splitlines()
        if line.strip()
    )[:MAX_SOURCE_CHARS]
    if len(text) < 80:
        raise SourceError("На странице недостаточно текста, чтобы составить рецепт.")
    return SourceDocument(
        source_type="website",
        title=title[:300],
        text=text,
        structured_recipe=recipes[0] if recipes else None,
        structured_recipes=tuple(recipes),
        cover_image_urls=cover_image_urls,
        step_image_urls=step_image_urls,
        recipe_cover_image_urls=tuple(
            tuple(_schema_image_urls(recipe.get("image"), final_url))
            for recipe in recipes
        ),
        recipe_step_image_urls=tuple(
            _structured_step_image_slots(recipe.get("recipeInstructions", []), final_url)
            for recipe in recipes
        ),
    )


def extract_youtube(url: str) -> SourceDocument:
    video_id = youtube_video_id(url)
    if not video_id:
        raise SourceError("Не удалось распознать ссылку на YouTube-видео.")
    try:
        transcript = YouTubeTranscriptApi().fetch(video_id, languages=["ru", "uk", "en"])
    except YouTubeTranscriptApiException as error:
        raise SourceError(
            "У видео нет доступных русских или английских субтитров. "
            "Автоматическое распознавание аудио пока не подключено."
        ) from error
    text = " ".join(snippet.text.strip() for snippet in transcript if snippet.text.strip())
    if len(text) < 80:
        raise SourceError("В субтитрах слишком мало текста, чтобы составить рецепт.")
    return SourceDocument(
        "youtube",
        _fetch_youtube_title(video_id) or f"YouTube {video_id}",
        text[:MAX_SOURCE_CHARS],
        cover_image_urls=(
            f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            f"https://i.ytimg.com/vi/{video_id}/sddefault.jpg",
            f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        ),
    )


def extract_source(url: str) -> SourceDocument:
    if youtube_video_id(url):
        return extract_youtube(url)
    return extract_website(url)

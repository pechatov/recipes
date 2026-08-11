from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import YouTubeTranscriptApiException

from .exceptions import SourceError


MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_CHARS = 60_000
MAX_REDIRECTS = 3
USER_AGENT = "FamilyRecipesImporter/1.0"


@dataclass(frozen=True)
class SourceDocument:
    source_type: str
    title: str
    text: str
    structured_recipe: dict[str, Any] | None = None


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


def _validate_public_url(url: str) -> None:
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
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise SourceError("Импорт из локальной или служебной сети запрещён.")


def _download_html(url: str) -> tuple[str, str]:
    current_url = url
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(timeout=20, follow_redirects=False, headers=headers) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _validate_public_url(current_url)
            try:
                with client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise SourceError("Сайт вернул перенаправление без нового адреса.")
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if "html" not in content_type:
                        raise SourceError("По ссылке не найдена HTML-страница с рецептом.")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_SOURCE_BYTES:
                            raise SourceError("Страница слишком большая для безопасного импорта.")
                        chunks.append(chunk)
                    encoding = response.encoding or "utf-8"
                    return b"".join(chunks).decode(encoding, errors="replace"), str(response.url)
            except httpx.HTTPStatusError as error:
                raise SourceError(f"Сайт вернул ошибку HTTP {error.response.status_code}.") from error
            except httpx.HTTPError as error:
                raise SourceError("Не удалось загрузить страницу с рецептом.") from error
    raise SourceError("Сайт перенаправляет запрос слишком много раз.")


def _find_recipe_json(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        recipe_type = value.get("@type")
        types = recipe_type if isinstance(recipe_type, list) else [recipe_type]
        if any(str(item).lower() == "recipe" for item in types):
            return value
        for nested in value.values():
            result = _find_recipe_json(nested)
            if result:
                return result
    elif isinstance(value, list):
        for nested in value:
            result = _find_recipe_json(nested)
            if result:
                return result
    return None


def _structured_recipe(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        recipe = _find_recipe_json(value)
        if recipe:
            return recipe
    return None


def extract_website(url: str) -> SourceDocument:
    html, _ = _download_html(url)
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        element.decompose()
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    heading = soup.find("h1")
    if heading:
        title = heading.get_text(" ", strip=True) or title
    text = "\n".join(
        line.strip()
        for line in soup.get_text("\n").splitlines()
        if line.strip()
    )[:MAX_SOURCE_CHARS]
    if len(text) < 80:
        raise SourceError("На странице недостаточно текста, чтобы составить рецепт.")
    return SourceDocument("website", title[:300], text, _structured_recipe(BeautifulSoup(html, "html.parser")))


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
    return SourceDocument("youtube", f"YouTube {video_id}", text[:MAX_SOURCE_CHARS])


def extract_source(url: str) -> SourceDocument:
    if youtube_video_id(url):
        return extract_youtube(url)
    return extract_website(url)

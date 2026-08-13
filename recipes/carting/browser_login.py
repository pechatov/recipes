from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx
from django.conf import settings


SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,128}")
ACCESS_PATH_PATTERN = re.compile(r"/browser-login/access/[A-Za-z0-9_-]{20,256}")


class BrowserLoginError(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.CART_BROWSER_CONTROL_URL and settings.CART_BROWSER_CONTROL_KEY)


def _request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    allow_not_found: bool = False,
) -> dict:
    if not is_configured():
        raise BrowserLoginError("Удалённый вход в Яндекс Еду ещё не настроен.")
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            response = client.request(
                method,
                f"{settings.CART_BROWSER_CONTROL_URL}{path}",
                headers={"Authorization": f"Bearer {settings.CART_BROWSER_CONTROL_KEY}"},
                json=payload,
            )
        if response.status_code == 409:
            raise BrowserLoginError(
                "Браузер сейчас занят другой сборкой или ручной проверкой. "
                "Попробуйте ещё раз чуть позже."
            )
        if allow_not_found and response.status_code == 404:
            return {"status": "already_closed"}
        response.raise_for_status()
        data = response.json()
    except BrowserLoginError:
        raise
    except (httpx.HTTPError, ValueError) as error:
        raise BrowserLoginError("Не удалось связаться с браузером для корзины.") from error
    if not isinstance(data, dict):
        raise BrowserLoginError("Сервис браузера вернул неожиданный ответ.")
    return data


def _validated_session_id(value: Any) -> str:
    session_id = str(value or "")
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise BrowserLoginError("Сервис браузера вернул неверный идентификатор сессии.")
    return session_id


def start_session(scope: str, lifetime_minutes: int, session_id: str) -> str:
    session_id = _validated_session_id(session_id)
    data = _request(
        "POST",
        "/v1/sessions",
        payload={
            "scope": scope,
            "lifetime_minutes": lifetime_minutes,
            "session_id": session_id,
        },
    )
    returned_id = _validated_session_id(data.get("session_id"))
    if returned_id != session_id:
        raise BrowserLoginError("Сервис браузера вернул чужой идентификатор сессии.")
    return returned_id


def issue_access(session_id: str) -> str:
    session_id = _validated_session_id(session_id)
    data = _request("POST", f"/v1/sessions/{session_id}/access")
    path = str(data.get("access_path") or "")
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise BrowserLoginError("Сервис браузера вернул небезопасную ссылку.")
    if not ACCESS_PATH_PATTERN.fullmatch(parsed.path):
        raise BrowserLoginError("Сервис браузера вернул неверную ссылку.")
    return parsed.path


def stop_session(session_id: str) -> None:
    session_id = _validated_session_id(session_id)
    # DELETE is deliberately idempotent: the Pi may have closed an expired
    # window just before the user pressed the completion button.
    _request("DELETE", f"/v1/sessions/{session_id}", allow_not_found=True)

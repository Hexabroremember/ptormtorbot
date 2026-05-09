"""Telegram Bot API helpers used by the FastAPI app."""
from __future__ import annotations

from io import BytesIO
import os
import time

import httpx

_profile_cache: dict[int, tuple[float, dict[str, str | None]]] = {}
_PROFILE_TTL_SECONDS = int(os.environ.get("TELEGRAM_PROFILE_CACHE_TTL_SECONDS", "21600"))

# Retries help intermittent Telegram API / TLS blips (reported ~50% miss rate without).
_TELEGRAM_HTTP_ATTEMPTS = max(1, min(6, int(os.environ.get("TELEGRAM_HTTP_ATTEMPTS", "3"))))


def _telegram_api_err(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            desc = data.get("description")
            err_code = data.get("error_code")
            if desc:
                return f"{desc}" + (f" ({err_code})" if err_code is not None else "")
    except Exception:
        pass
    return resp.text[:400]


def _bot_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def get_telegram_chat_profile(chat_id: int | str | None) -> dict[str, str | None]:
    """Fetch public-ish Telegram chat fields for logs; returns empty values on failure."""
    token = _bot_token()
    if not token or not chat_id:
        return {}
    try:
        uid = int(chat_id)
    except (TypeError, ValueError):
        return {}
    now = time.time()
    cached = _profile_cache.get(uid)
    if cached and now - cached[0] < _PROFILE_TTL_SECONDS:
        return cached[1]
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"https://api.telegram.org/bot{token}/getChat",
                params={"chat_id": uid},
            )
            if not resp.is_success:
                profile: dict[str, str | None] = {}
            else:
                data = resp.json()
                result = data.get("result") if isinstance(data, dict) else None
                profile = {
                    "username": result.get("username") if isinstance(result, dict) else None,
                    "first_name": result.get("first_name") if isinstance(result, dict) else None,
                    "last_name": result.get("last_name") if isinstance(result, dict) else None,
                }
    except Exception:
        profile = {}
    _profile_cache[uid] = (now, profile)
    return profile


def send_telegram_message(chat_id: int | str | None, text: str) -> tuple[bool, str | None]:
    token = _bot_token()
    if not token or not chat_id:
        return False, "telegram_bot_token_or_chat_missing"
    last_err: str | None = None
    for attempt in range(_TELEGRAM_HTTP_ATTEMPTS):
        if attempt:
            time.sleep(0.35 * (2 ** (attempt - 1)))
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                )
                if resp.is_success:
                    return True, None
                last_err = _telegram_api_err(resp)
        except Exception as exc:  # noqa: BLE001 - notification failure must not break payment flow
            last_err = str(exc)
    return False, last_err


def send_telegram_document_url(
    chat_id: int | str | None,
    document_url: str,
    *,
    caption: str | None = None,
) -> tuple[bool, str | None]:
    """Send a document by URL — Telegram fetches the file itself (no multipart upload needed)."""
    token = _bot_token()
    if not token or not chat_id:
        return False, "telegram_bot_token_or_chat_missing"
    if not document_url:
        return False, "document_url_missing"
    last_err: str | None = None
    for attempt in range(_TELEGRAM_HTTP_ATTEMPTS):
        if attempt:
            time.sleep(0.35 * (2 ** (attempt - 1)))
        try:
            with httpx.Client(timeout=60) as client:
                payload: dict[str, str | int] = {
                    "chat_id": chat_id,
                    "document": document_url,
                }
                if caption:
                    payload["caption"] = caption
                resp = client.post(
                    f"https://api.telegram.org/bot{token}/sendDocument",
                    json=payload,
                )
                if resp.is_success:
                    return True, None
                last_err = _telegram_api_err(resp)
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
    return False, last_err


def send_telegram_photo_bytes(
    chat_id: int | str | None,
    photo_bytes: bytes,
    *,
    filename: str = "preview.jpg",
    caption: str | None = None,
) -> tuple[bool, str | None]:
    """Upload a JPEG/PNG as sendPhoto (multipart)."""
    token = _bot_token()
    if not token or not chat_id:
        return False, "telegram_bot_token_or_chat_missing"
    last_err: str | None = None
    for attempt in range(_TELEGRAM_HTTP_ATTEMPTS):
        if attempt:
            time.sleep(0.35 * (2 ** (attempt - 1)))
        try:
            with httpx.Client(timeout=120) as client:
                files = {"photo": (filename, BytesIO(photo_bytes), "image/jpeg")}
                data: dict[str, str | int] = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                resp = client.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data=data,
                    files=files,
                )
                if resp.is_success:
                    return True, None
                last_err = _telegram_api_err(resp)
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
    return False, last_err


def send_telegram_document(
    chat_id: int | str | None,
    pdf_bytes: bytes,
    *,
    filename: str,
    caption: str | None = None,
) -> tuple[bool, str | None]:
    """Send a document by uploading bytes (multipart). Prefer send_telegram_document_url when possible."""
    token = _bot_token()
    if not token or not chat_id:
        return False, "telegram_bot_token_or_chat_missing"
    last_err: str | None = None
    for attempt in range(_TELEGRAM_HTTP_ATTEMPTS):
        if attempt:
            time.sleep(0.35 * (2 ** (attempt - 1)))
        try:
            with httpx.Client(timeout=120) as client:
                files = {
                    "document": (filename, BytesIO(pdf_bytes), "application/pdf"),
                }
                data: dict[str, str | int] = {
                    "chat_id": chat_id,
                }
                if caption:
                    data["caption"] = caption
                resp = client.post(
                    f"https://api.telegram.org/bot{token}/sendDocument",
                    data=data,
                    files=files,
                )
                if resp.is_success:
                    return True, None
                last_err = _telegram_api_err(resp)
        except Exception as exc:  # noqa: BLE001 - notification failure must not break payment flow
            last_err = str(exc)
    return False, last_err

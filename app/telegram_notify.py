"""Telegram Bot API helpers used by the FastAPI app."""
from __future__ import annotations

from io import BytesIO
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

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
        logger.debug(
            "[telegram:api] getChat skipped chat_id=%s token_configured=%s",
            chat_id,
            bool(token),
        )
        return {}
    try:
        uid = int(chat_id)
    except (TypeError, ValueError):
        logger.debug("[telegram:api] getChat skipped invalid chat_id=%r", chat_id)
        return {}
    now = time.time()
    cached = _profile_cache.get(uid)
    if cached and now - cached[0] < _PROFILE_TTL_SECONDS:
        logger.debug("[telegram:api] getChat cache hit telegram_user_id=%s", uid)
        return cached[1]
    logger.debug("[telegram:api] getChat request telegram_user_id=%s", uid)
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"https://api.telegram.org/bot{token}/getChat",
                params={"chat_id": uid},
            )
            if not resp.is_success:
                logger.warning(
                    "[telegram:api] getChat failed telegram_user_id=%s status=%s err=%s",
                    uid,
                    resp.status_code,
                    _telegram_api_err(resp),
                )
                profile: dict[str, str | None] = {}
            else:
                data = resp.json()
                result = data.get("result") if isinstance(data, dict) else None
                profile = {
                    "username": result.get("username") if isinstance(result, dict) else None,
                    "first_name": result.get("first_name") if isinstance(result, dict) else None,
                    "last_name": result.get("last_name") if isinstance(result, dict) else None,
                }
                logger.debug(
                    "[telegram:api] getChat ok telegram_user_id=%s has_username=%s",
                    uid,
                    bool(profile.get("username")),
                )
    except Exception:
        logger.exception("[telegram:api] getChat exception telegram_user_id=%s", uid)
        profile = {}
    _profile_cache[uid] = (now, profile)
    return profile


def send_telegram_message(chat_id: int | str | None, text: str) -> tuple[bool, str | None]:
    token = _bot_token()
    if not token or not chat_id:
        logger.warning(
            "[telegram:delivery] sendMessage skipped chat_id=%s token_configured=%s",
            chat_id,
            bool(token),
        )
        return False, "telegram_bot_token_or_chat_missing"
    logger.debug(
        "[telegram:delivery] sendMessage start chat_id=%s text_len=%s attempts=%s",
        chat_id,
        len(text or ""),
        _TELEGRAM_HTTP_ATTEMPTS,
    )
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
                    logger.info(
                        "[telegram:delivery] sendMessage ok chat_id=%s attempt=%s",
                        chat_id,
                        attempt + 1,
                    )
                    return True, None
                last_err = _telegram_api_err(resp)
                logger.warning(
                    "[telegram:delivery] sendMessage http_error chat_id=%s attempt=%s err=%s",
                    chat_id,
                    attempt + 1,
                    last_err,
                )
        except Exception as exc:  # noqa: BLE001 - notification failure must not break payment flow
            last_err = str(exc)
            logger.warning(
                "[telegram:delivery] sendMessage exception chat_id=%s attempt=%s err=%s",
                chat_id,
                attempt + 1,
                last_err,
                exc_info=True,
            )
    logger.error(
        "[telegram:delivery] sendMessage failed after retries chat_id=%s last_err=%s",
        chat_id,
        last_err,
    )
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
        logger.warning(
            "[telegram:delivery] sendDocument(URL) skipped chat_id=%s token_configured=%s",
            chat_id,
            bool(token),
        )
        return False, "telegram_bot_token_or_chat_missing"
    if not document_url:
        logger.warning("[telegram:delivery] sendDocument(URL) skipped document_url_missing chat_id=%s", chat_id)
        return False, "document_url_missing"
    safe_ref = document_url.split("?", 1)[0]
    if len(safe_ref) > 120:
        safe_ref = safe_ref[:60] + "…" + safe_ref[-40:]
    logger.debug(
        "[telegram:delivery] sendDocument(URL) start chat_id=%s url_path=%s bytes_hint=%s",
        chat_id,
        safe_ref,
        len(document_url),
    )
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
                    logger.info(
                        "[telegram:delivery] sendDocument(URL) ok chat_id=%s attempt=%s",
                        chat_id,
                        attempt + 1,
                    )
                    return True, None
                last_err = _telegram_api_err(resp)
                logger.warning(
                    "[telegram:delivery] sendDocument(URL) http_error chat_id=%s attempt=%s err=%s",
                    chat_id,
                    attempt + 1,
                    last_err,
                )
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            logger.warning(
                "[telegram:delivery] sendDocument(URL) exception chat_id=%s attempt=%s",
                chat_id,
                attempt + 1,
                exc_info=True,
            )
    logger.error(
        "[telegram:delivery] sendDocument(URL) failed chat_id=%s last_err=%s",
        chat_id,
        last_err,
    )
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
        logger.warning(
            "[telegram:delivery] sendPhoto skipped chat_id=%s token_configured=%s",
            chat_id,
            bool(token),
        )
        return False, "telegram_bot_token_or_chat_missing"
    logger.debug(
        "[telegram:delivery] sendPhoto start chat_id=%s filename=%s bytes=%s",
        chat_id,
        filename,
        len(photo_bytes or b""),
    )
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
                    logger.info(
                        "[telegram:delivery] sendPhoto ok chat_id=%s attempt=%s",
                        chat_id,
                        attempt + 1,
                    )
                    return True, None
                last_err = _telegram_api_err(resp)
                logger.warning(
                    "[telegram:delivery] sendPhoto http_error chat_id=%s attempt=%s err=%s",
                    chat_id,
                    attempt + 1,
                    last_err,
                )
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            logger.warning(
                "[telegram:delivery] sendPhoto exception chat_id=%s attempt=%s",
                chat_id,
                attempt + 1,
                exc_info=True,
            )
    logger.error(
        "[telegram:delivery] sendPhoto failed chat_id=%s last_err=%s",
        chat_id,
        last_err,
    )
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
        logger.warning(
            "[telegram:delivery] sendDocument(bytes) skipped chat_id=%s token_configured=%s",
            chat_id,
            bool(token),
        )
        return False, "telegram_bot_token_or_chat_missing"
    logger.debug(
        "[telegram:delivery] sendDocument(bytes) start chat_id=%s filename=%s bytes=%s",
        chat_id,
        filename,
        len(pdf_bytes or b""),
    )
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
                    logger.info(
                        "[telegram:delivery] sendDocument(bytes) ok chat_id=%s attempt=%s",
                        chat_id,
                        attempt + 1,
                    )
                    return True, None
                last_err = _telegram_api_err(resp)
                logger.warning(
                    "[telegram:delivery] sendDocument(bytes) http_error chat_id=%s attempt=%s err=%s",
                    chat_id,
                    attempt + 1,
                    last_err,
                )
        except Exception as exc:  # noqa: BLE001 - notification failure must not break payment flow
            last_err = str(exc)
            logger.warning(
                "[telegram:delivery] sendDocument(bytes) exception chat_id=%s attempt=%s",
                chat_id,
                attempt + 1,
                exc_info=True,
            )
    logger.error(
        "[telegram:delivery] sendDocument(bytes) failed chat_id=%s last_err=%s",
        chat_id,
        last_err,
    )
    return False, last_err

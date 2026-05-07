"""Telegram Bot API helpers used by the FastAPI app."""
from __future__ import annotations

from io import BytesIO
import os

import httpx


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


def send_telegram_message(chat_id: int | str | None, text: str) -> tuple[bool, str | None]:
    token = _bot_token()
    if not token or not chat_id:
        return False, "telegram_bot_token_or_chat_missing"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
            if resp.is_success:
                return True, None
            return False, _telegram_api_err(resp)
    except Exception as exc:  # noqa: BLE001 - notification failure must not break payment flow
        return False, str(exc)


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
    try:
        with httpx.Client(timeout=45) as client:
            payload: dict[str, str | int] = {
                "chat_id": chat_id,
                "document": document_url,
                "parse_mode": "HTML",
            }
            if caption:
                payload["caption"] = caption
            resp = client.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                json=payload,
            )
            if resp.is_success:
                return True, None
            return False, _telegram_api_err(resp)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


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
    try:
        with httpx.Client(timeout=90) as client:
            files = {
                "document": (filename, BytesIO(pdf_bytes), "application/pdf"),
            }
            data: dict[str, str | int] = {
                "chat_id": chat_id,
                "parse_mode": "HTML",
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
            return False, _telegram_api_err(resp)
    except Exception as exc:  # noqa: BLE001 - notification failure must not break payment flow
        return False, str(exc)

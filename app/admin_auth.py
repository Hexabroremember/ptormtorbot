"""Admin authentication for Telegram Web Apps and emergency API-key access."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, Request, status


@dataclass(frozen=True)
class TelegramWebAppUser:
    id: int
    first_name: str | None = None
    username: str | None = None


@dataclass(frozen=True)
class AdminIdentity:
    auth_method: str
    telegram_user: TelegramWebAppUser | None = None


# Primary owner — always treated as admin even if ADMIN_TELEGRAM_IDS omits them (prevents lockout).
PRIMARY_OWNER_TELEGRAM_ID = 5319095718


def admin_ids() -> set[int]:
    ids: set[int] = {PRIMARY_OWNER_TELEGRAM_ID}

    owner_raw = os.environ.get("BOT_OWNER_TELEGRAM_ID", "").strip()
    if owner_raw:
        try:
            ids.add(int(owner_raw))
        except ValueError:
            pass

    admins_raw = os.environ.get("ADMIN_TELEGRAM_IDS", "").strip()
    for piece in admins_raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            ids.add(int(piece))
        except ValueError:
            continue

    return ids


def verify_telegram_init_data(init_data: str) -> TelegramWebAppUser:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="bot_token_missing")
    parsed = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received_hash = parsed.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="telegram_hash_missing")

    data_check_string = "\n".join(f"{key}={parsed[key]}" for key in sorted(parsed))
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="telegram_auth_invalid")

    auth_date_raw = parsed.get("auth_date", "0")
    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        auth_date = 0
    if auth_date and time.time() - auth_date > 7 * 24 * 60 * 60:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="telegram_auth_expired")

    user_raw = parsed.get("user")
    if not user_raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="telegram_user_missing")
    try:
        user: dict[str, Any] = json.loads(user_raw)
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="telegram_user_invalid") from exc
    return TelegramWebAppUser(
        id=user_id,
        first_name=user.get("first_name"),
        username=user.get("username"),
    )


def parse_optional_telegram_user(init_data: str | None) -> TelegramWebAppUser | None:
    if not init_data:
        return None
    try:
        return verify_telegram_init_data(init_data)
    except HTTPException:
        return None


def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
) -> AdminIdentity:
    init_data = (x_telegram_init_data or "").strip() or request.headers.get("x-telegram-init-data", "").strip()
    if init_data:
        user = verify_telegram_init_data(init_data)
        if user.id in admin_ids():
            return AdminIdentity(auth_method="telegram", telegram_user=user)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_only")

    secret = os.environ.get("ADMIN_API_SECRET", "").strip()
    if secret and authorization == f"Bearer {secret}":
        return AdminIdentity(auth_method="api_key")

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="admin_auth_required")

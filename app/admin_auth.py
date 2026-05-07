"""Admin authentication for Telegram Web Apps and emergency API-key access."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote

from fastapi import Header, HTTPException, Query, Request, status


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


def _extract_webapp_init_data(
    *,
    request: Request,
    authorization: str | None,
    x_telegram_init_data: str | None,
    tg_init_data: str | None,
    body_init_data: str | None = None,
) -> str | None:
    """Collect initData from headers, JSON body, query, or Telegram-style Authorization: TMA <data>."""
    raw = (
        (x_telegram_init_data or "").strip()
        or request.headers.get("x-telegram-init-data", "").strip()
        or request.headers.get("X-Telegram-Init-Data", "").strip()
        or (body_init_data or "").strip()
        or (tg_init_data or "").strip()
    )
    if raw:
        return unquote(raw)
    auth = (authorization or "").strip()
    if auth.lower().startswith("tma "):
        return auth[4:].strip()
    return None


def extract_webapp_init_data(
    request: Request,
    *,
    x_telegram_init_data: str | None = None,
    tg_init_data_query: str | None = None,
    body_init_data: str | None = None,
    authorization: str | None = None,
) -> str | None:
    """Public helper for Mini App API routes (same sources as admin auth + POST body fallback)."""
    return _extract_webapp_init_data(
        request=request,
        authorization=authorization,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data=tg_init_data_query,
        body_init_data=body_init_data,
    )


# Signed URL session: bot embeds tg_sess in Web App URL so the panel authenticates as Telegram
# when initData is empty (some clients). Bound to admin user id + expiry; HMAC with bot token.
TG_SESS_TTL_SEC = int(os.environ.get("ADMIN_TG_SESS_TTL_SEC", str(7 * 24 * 60 * 60)))


def _admin_tg_sess_key() -> bytes:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return b""
    return hashlib.sha256(b"admin_tg_sess_v1:" + token.encode("utf-8")).digest()


def mint_admin_tg_sess(telegram_user_id: int) -> str:
    """Return URL-safe token proving this user opened /admin from our bot (until expiry)."""
    if not _admin_tg_sess_key():
        return ""
    exp = int(time.time()) + max(60, TG_SESS_TTL_SEC)
    body = f"{telegram_user_id}:{exp}"
    digest = hmac.new(_admin_tg_sess_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{body}:{digest}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def verify_admin_tg_sess(token: str) -> TelegramWebAppUser | None:
    """Validate tg_sess query param; returns user if admin and not expired."""
    if not token or not _admin_tg_sess_key():
        return None
    try:
        pad = "=" * ((4 - len(token) % 4) % 4)
        decoded = base64.urlsafe_b64decode(token + pad).decode("utf-8")
        body, digest = decoded.rsplit(":", 1)
        uid_s, exp_s = body.split(":", 1)
        uid = int(uid_s)
        exp = int(exp_s)
    except (ValueError, UnicodeDecodeError):
        return None
    if time.time() > exp:
        return None
    expected = hmac.new(_admin_tg_sess_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        return None
    if uid not in admin_ids():
        return None
    return TelegramWebAppUser(id=uid)


def effective_admin_secret() -> str:
    """Return ADMIN_API_SECRET if set; otherwise derive a stable secret from the bot token.

    This ensures there is always a working fallback secret even when the env var
    is not explicitly configured — the derived secret is deterministic and safe
    as long as TELEGRAM_BOT_TOKEN itself is kept private.
    """
    explicit = os.environ.get("ADMIN_API_SECRET", "").strip()
    if explicit:
        return explicit
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        return hashlib.sha256(f"admin_panel_secret:{token}".encode("utf-8")).hexdigest()[:32]
    return ""


def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    tg_init_data: str | None = Query(default=None, description="Fallback when proxies strip custom headers"),
    tg_sess: str | None = Query(default=None, description="HMAC session from bot Web App URL when initData missing"),
) -> AdminIdentity:
    init_data = _extract_webapp_init_data(
        request=request,
        authorization=authorization,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data=tg_init_data,
        body_init_data=None,
    )
    if init_data:
        user = verify_telegram_init_data(init_data)
        if user.id in admin_ids():
            return AdminIdentity(auth_method="telegram", telegram_user=user)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_only")

    sess_raw = (tg_sess or "").strip()
    if sess_raw:
        sess_user = verify_admin_tg_sess(unquote(sess_raw))
        if sess_user:
            return AdminIdentity(auth_method="telegram_sess", telegram_user=sess_user)

    secret = effective_admin_secret()
    if secret and authorization == f"Bearer {secret}":
        return AdminIdentity(auth_method="api_key")

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="admin_auth_required")

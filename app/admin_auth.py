"""Admin authentication for Telegram Web Apps and emergency API-key access."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote

from fastapi import Header, HTTPException, Query, Request, status

logger = logging.getLogger(__name__)


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


def _init_data_max_age_sec() -> int:
    """Upper bound on how old WebApp ``auth_date`` may be (seconds).

    Default 30 days so purchase history and other APIs keep working after long gaps;
    set ``TELEGRAM_INIT_DATA_MAX_AGE_SEC`` for a stricter window (minimum enforced: 300).
    """
    raw = os.environ.get("TELEGRAM_INIT_DATA_MAX_AGE_SEC", "").strip()
    if raw:
        try:
            return max(300, int(raw))
        except ValueError:
            pass
    return 30 * 24 * 60 * 60


def verify_telegram_init_data(init_data: str) -> TelegramWebAppUser:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.warning("[telegram:session] initData verify rejected detail=bot_token_missing")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="bot_token_missing")
    parsed = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received_hash = parsed.pop("hash", "")
    if not received_hash:
        logger.warning("[telegram:session] initData verify rejected detail=telegram_hash_missing")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="telegram_hash_missing")

    data_check_string = "\n".join(f"{key}={parsed[key]}" for key in sorted(parsed))
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        logger.warning("[telegram:session] initData verify rejected detail=telegram_auth_invalid")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="telegram_auth_invalid")

    auth_date_raw = parsed.get("auth_date", "0")
    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        auth_date = 0
    if auth_date and time.time() - auth_date > _init_data_max_age_sec():
        logger.warning(
            "[telegram:session] initData verify rejected detail=telegram_auth_expired auth_age_sec=%s max_age_sec=%s",
            int(time.time() - auth_date) if auth_date else 0,
            _init_data_max_age_sec(),
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="telegram_auth_expired")

    user_raw = parsed.get("user")
    if not user_raw:
        logger.warning("[telegram:session] initData verify rejected detail=telegram_user_missing")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="telegram_user_missing")
    try:
        user: dict[str, Any] = json.loads(user_raw)
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("[telegram:session] initData verify rejected detail=telegram_user_invalid")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="telegram_user_invalid") from exc
    out = TelegramWebAppUser(
        id=user_id,
        first_name=user.get("first_name"),
        username=user.get("username"),
    )
    logger.debug("[telegram:session] initData verified telegram_user_id=%s", out.id)
    return out


def parse_optional_telegram_user(init_data: str | None) -> TelegramWebAppUser | None:
    if not init_data:
        return None
    try:
        return verify_telegram_init_data(init_data)
    except HTTPException:
        return None


def resolve_telegram_webapp_user(
    request: Request,
    *,
    x_telegram_init_data: str | None = None,
    tg_init_data_query: str | None = None,
    body_init_data: str | None = None,
    tg_user_sess: str | None = None,
    authorization: str | None = None,
) -> TelegramWebAppUser | None:
    """Verify Mini App user from initData, trying several sources.

    Order matters: JSON body ``telegram_init_data`` is checked first when present.
    Some proxies and hosts strip ``X-Telegram-Init-Data`` but the POST body survives,
    which would otherwise leave chat_id unresolved while PDF generation still works.
    """
    auth_hdr = (
        (authorization or "").strip()
        or (request.headers.get("Authorization") or request.headers.get("authorization") or "").strip()
    )
    candidates: list[str] = []

    def add(raw: str | None) -> None:
        s = (raw or "").strip()
        if not s:
            return
        candidates.append(unquote(s))

    add(body_init_data)
    add(x_telegram_init_data)
    add(request.headers.get("x-telegram-init-data"))
    add(request.headers.get("X-Telegram-Init-Data"))
    add(tg_init_data_query)
    if auth_hdr.lower().startswith("tma "):
        add(auth_hdr[4:])

    seen: set[str] = set()
    for raw in candidates:
        if raw in seen:
            continue
        seen.add(raw)
        user = parse_optional_telegram_user(raw)
        if user:
            logger.debug(
                "[telegram:session] resolved via initData path=%s method=%s telegram_user_id=%s",
                request.url.path,
                request.method,
                user.id,
            )
            return user

    sess = (
        (tg_user_sess or "").strip()
        or request.query_params.get("tg_user_sess", "").strip()
        or request.headers.get("x-telegram-user-sess", "").strip()
    )
    if sess:
        logger.debug(
            "[telegram:session] trying tg_user_sess fallback path=%s init_candidates_tried=%s",
            request.url.path,
            len(seen),
        )
    user = verify_user_tg_sess(sess)
    if user:
        logger.debug(
            "[telegram:session] resolved via tg_user_sess path=%s telegram_user_id=%s",
            request.url.path,
            user.id,
        )
        return user
    if sess:
        logger.debug(
            "[telegram:session] unresolved after tg_user_sess path=%s (invalid, expired, or unsigned)",
            request.url.path,
        )
    else:
        logger.debug(
            "[telegram:session] unresolved path=%s init_candidates_tried=%s no_tg_user_sess",
            request.url.path,
            len(seen),
        )
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
TG_USER_SESS_TTL_SEC = int(os.environ.get("TG_USER_SESS_TTL_SEC", str(30 * 24 * 60 * 60)))


def _admin_tg_sess_key() -> bytes:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return b""
    return hashlib.sha256(b"admin_tg_sess_v1:" + token.encode("utf-8")).digest()


def _user_tg_sess_key() -> bytes:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return b""
    return hashlib.sha256(b"user_tg_sess_v1:" + token.encode("utf-8")).digest()


def mint_admin_tg_sess(telegram_user_id: int) -> str:
    """Return URL-safe token proving this user opened /admin from our bot (until expiry)."""
    if not _admin_tg_sess_key():
        return ""
    exp = int(time.time()) + max(60, TG_SESS_TTL_SEC)
    body = f"{telegram_user_id}:{exp}"
    digest = hmac.new(_admin_tg_sess_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{body}:{digest}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def mint_user_tg_sess(telegram_user_id: int) -> str:
    """Return URL-safe token proving this user opened the Mini App from our bot."""
    if not _user_tg_sess_key():
        logger.warning(
            "[telegram:session] mint_user_tg_sess unavailable (TELEGRAM_BOT_TOKEN missing) telegram_user_id=%s",
            telegram_user_id,
        )
        return ""
    exp = int(time.time()) + max(60, TG_USER_SESS_TTL_SEC)
    body = f"{telegram_user_id}:{exp}"
    digest = hmac.new(_user_tg_sess_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
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


def verify_user_tg_sess(token: str) -> TelegramWebAppUser | None:
    """Validate a public Mini App user session token minted by the bot."""
    if not token or not _user_tg_sess_key():
        if token and not _user_tg_sess_key():
            logger.debug("[telegram:session] tg_user_sess verify skipped (no signing key / bot token)")
        return None
    try:
        pad = "=" * ((4 - len(token) % 4) % 4)
        decoded = base64.urlsafe_b64decode(token + pad).decode("utf-8")
        body, digest = decoded.rsplit(":", 1)
        uid_s, exp_s = body.split(":", 1)
        uid = int(uid_s)
        exp = int(exp_s)
    except (ValueError, UnicodeDecodeError):
        logger.debug("[telegram:session] tg_user_sess malformed (decode failed)")
        return None
    if time.time() > exp:
        logger.debug(
            "[telegram:session] tg_user_sess expired telegram_user_id=%s exp_epoch=%s",
            uid,
            exp,
        )
        return None
    expected = hmac.new(_user_tg_sess_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        logger.debug("[telegram:session] tg_user_sess signature mismatch")
        return None
    logger.debug("[telegram:session] tg_user_sess valid telegram_user_id=%s", uid)
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
            logger.debug(
                "[telegram:admin] ok via initData telegram_user_id=%s path=%s",
                user.id,
                request.url.path,
            )
            return AdminIdentity(auth_method="telegram", telegram_user=user)
        logger.warning(
            "[telegram:admin] forbidden not_admin telegram_user_id=%s path=%s",
            user.id,
            request.url.path,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_only")

    sess_raw = (tg_sess or "").strip()
    if sess_raw:
        sess_user = verify_admin_tg_sess(unquote(sess_raw))
        if sess_user:
            logger.debug(
                "[telegram:admin] ok via tg_sess telegram_user_id=%s path=%s",
                sess_user.id,
                request.url.path,
            )
            return AdminIdentity(auth_method="telegram_sess", telegram_user=sess_user)

    secret = effective_admin_secret()
    if secret and authorization == f"Bearer {secret}":
        logger.debug("[telegram:admin] ok via api_key path=%s", request.url.path)
        return AdminIdentity(auth_method="api_key")

    logger.info(
        "[telegram:admin] auth failed admin_auth_required path=%s method=%s had_tg_sess_param=%s",
        request.url.path,
        request.method,
        bool(sess_raw),
    )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="admin_auth_required")

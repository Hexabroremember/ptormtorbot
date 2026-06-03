from __future__ import annotations

from io import BytesIO
import logging
import os
import time
import re
import secrets
import uuid
from pathlib import Path
from typing import Any

import fitz
import qrcode
from qrcode.constants import ERROR_CORRECT_M
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field
from telegram import Update as TelegramUpdate

from app.activity_store import (
    get_event,
    get_payment_redeem_event_for_user,
    list_events,
    list_payment_redeems_for_user,
    list_user_directory,
    log_event,
    summary as activity_summary,
)
from app.admin_auth import (
    ADMIN_TG_SESS_REFRESH_GRACE_SEC,
    AdminIdentity,
    admin_ids,
    effective_admin_secret,
    mint_admin_tg_sess,
    mint_user_tg_sess,
    require_admin,
    resolve_telegram_webapp_user,
    verify_admin_tg_sess,
    verify_telegram_init_data,
    verify_user_tg_sess,
)
from app.admin_control import get_control_state, maintenance_mode_enabled, set_maintenance_mode
from app.crypto_orders import (
    create_order,
    get_order,
    list_orders,
    list_orders_for_admin,
    list_paid_orders_for_user,
    mark_paid,
    mark_pdf_sent,
)
from app.coupons_store import (
    create_coupon,
    list_coupons,
    normalize_coupon_code,
    record_coupon_use,
    set_coupon_active,
    summary as coupons_summary,
    validate_coupon,
)
from app.nowpayments import create_invoice as nowpayments_create_invoice, verify_ipn_signature
from app.payment_codes_store import (
    issue_new_code,
    mark_code_telegram_pdf_sent,
    normalize_code,
    redeem_code,
)
from app.payment_code_meta import TIER_LABELS as EXPIRY_TIER_LABELS
from app.payment_code_meta import VALID_TIER_KEYS
from app.pdf_download_cache import get_pdf_record, register_pdf_bytes
from app.public_url import effective_public_base_url
from app.request_logging import StructuredLoggingMiddleware
from app.rate_limits import check_rate_limit, delete_override, list_overrides, upsert_override
from app.storage_connection import connect_storage, qp
from app.telegram_notify import (
    get_telegram_chat_profile,
    send_telegram_document,
    send_telegram_document_url,
    send_telegram_message,
)
from app.telegram_users_store import (
    list_users as list_telegram_users,
    send_broadcast,
    summary as telegram_users_summary,
    upsert_from_telegram_user,
)
from app.user_saved_forms import delete_for_user, list_for_user, upsert_for_user


ROOT_DIR = Path(__file__).resolve().parents[1]
ASSETS_DIR = ROOT_DIR / "assets"
FONTS_DIR = ROOT_DIR / "fonts"
STATIC_DIR = ROOT_DIR / "static"
DIST_DIR = ROOT_DIR / "dist"
DIST_ASSETS_DIR = DIST_DIR / "assets"

OUTPUT_PDF_FILENAME = "FormPDFPreview.pdf"

logger = logging.getLogger(__name__)

_TELEGRAM_USER_REQUIRED_DETAIL: dict[str, Any] = {
    "code": "telegram_user_required",
    "hint": (
        "Open the Mini App inside Telegram (keyboard or bot link with tg_user_sess). "
        "A normal browser tab cannot send Telegram initData."
    ),
}


class WatermarkMissingError(Exception):
    """Raised when ``watermark=True`` but no ``watermark.png`` exists on disk."""

    def __init__(self) -> None:
        super().__init__(
            "סימן מים מופעל אך הקובץ watermark.png לא נמצא בשרת. "
            "יש להוסיף את הקובץ כ־assets/watermark.png או watermark.png בשורש הפרויקט "
            "(וב־Docker לכלול אותו ב־COPY)."
        )


TEMPLATE_PDF = ASSETS_DIR / "template.pdf"
WATERMARK_PNG_ASSETS = ASSETS_DIR / "watermark.png"
WATERMARK_PNG_ROOT = ROOT_DIR / "watermark.png"
ARIMO_FONT = FONTS_DIR / "Arimo-Bold.ttf"
HEBREW_IMAGE_FONT = FONTS_DIR / "Arial-Bold.ttf"

HEBREW_NAME_RECT = fitz.Rect(239.4, 535.8, 398.1, 559.1)
ENGLISH_NAME_RECT = fitz.Rect(159.3, 562.6, 398.1, 585.8)
HEBREW_RIGHT_EDGE = 398.13
HEBREW_TOP = 535.85
ENGLISH_BASELINE = fitz.Point(159.31, 580.62)
NAME_FONT_SIZE = 23.25

ID_NUMBER_RECT = fitz.Rect(194.05, 629.88, 306.64, 652.38)
EXPIRATION_RECT = fitz.Rect(194.84, 660.68, 307.39, 683.18)
ID_NUMBER_BASELINE = fitz.Point(194.05, 647.34)
EXPIRATION_BASELINE = fitz.Point(194.84, 678.14)
DATA_FONT_SIZE = 22.5

# Embedded QR beside the name block (fallback if auto-detection finds no square image).
QR_RECT_FALLBACK = fitz.Rect(405.638, 511.943, 500.138, 606.443)

# Static QR for every generated PDF (never chosen per request).
# - If assets/qr.png exists, that image is scaled into the QR box (exact pixels preserved).
# - Else encode one line from assets/qr_payload.txt, else DEFAULT_STATIC_QR_PAYLOAD.
STATIC_QR_PNG = ASSETS_DIR / "qr.png"
STATIC_QR_PAYLOAD_TXT = ASSETS_DIR / "qr_payload.txt"
DEFAULT_STATIC_QR_PAYLOAD = (
    "https://www.btl.gov.il/Pages/default.aspx#xxxxxxxxxxpoex02xxxxxxxxxxxxxxxxxxxxxxx"
)


class GeneratePdfRequest(BaseModel):
    hebrew_full_name: str = Field(..., min_length=1, max_length=80)
    english_full_name: str = Field(..., min_length=1, max_length=80)
    id_number: str = Field(..., min_length=1, max_length=24)
    expiration_date: str = Field(..., min_length=1, max_length=24)
    watermark: bool = False
    telegram_init_data: str | None = Field(default=None, max_length=16000)
    telegram_user_session: str | None = Field(default=None, max_length=1000)


class RedeemFormSnapshot(BaseModel):
    hebrew_full_name: str | None = Field(default=None, max_length=120)
    english_full_name: str | None = Field(default=None, max_length=120)
    id_number: str | None = Field(default=None, max_length=32)
    expiration_date: str | None = Field(default=None, max_length=48)
    expiry_option: str | None = Field(default=None, max_length=32)


class RedeemPaymentCodeRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=64)
    form: RedeemFormSnapshot | None = None
    telegram_init_data: str | None = Field(default=None, max_length=16000)
    telegram_user_session: str | None = Field(default=None, max_length=1000)


class MaintenanceModeRequest(BaseModel):
    enabled: bool


class AdminIssueCodesRequest(BaseModel):
    """Counts per type: key ``global`` and/or ``300`` … ``1500`` (tier ids)."""

    bulk: dict[str, int] = Field(default_factory=dict)


class CreateCryptoInvoiceRequest(BaseModel):
    price_ils: float = Field(..., gt=0, le=50_000)
    expiry_option: str | None = Field(default=None, max_length=32)
    coupon_code: str | None = Field(default=None, max_length=64)
    # Ignored for authorization: identity comes only from verified initData or tg_user_sess.
    telegram_user_id: int | None = None
    username: str | None = Field(default=None, max_length=64)
    first_name: str | None = Field(default=None, max_length=64)
    form: RedeemFormSnapshot | None = None
    telegram_init_data: str | None = Field(default=None, max_length=16000)
    telegram_user_session: str | None = Field(default=None, max_length=1000)


class ManualPaymentRequest(BaseModel):
    method: str = Field(..., min_length=2, max_length=32)
    price_ils: float = Field(..., gt=0, le=50_000)
    final_price_ils: float | None = Field(default=None, gt=0, le=50_000)
    discount_ils: float | None = Field(default=None, ge=0, le=50_000)
    coupon_code: str | None = Field(default=None, max_length=64)
    expiry_option: str | None = Field(default=None, max_length=32)
    form: RedeemFormSnapshot | None = None
    telegram_init_data: str | None = Field(default=None, max_length=16000)
    telegram_user_session: str | None = Field(default=None, max_length=1000)


class SavedFormSnapshot(BaseModel):
    fullName: str | None = Field(default="", max_length=120)
    fullNameEn: str | None = Field(default="", max_length=120)
    idNumber: str | None = Field(default="", max_length=32)
    expiryOption: str | None = Field(default="", max_length=32)
    birthDate: str | None = Field(default="", max_length=32)
    idIssueDate: str | None = Field(default="", max_length=32)


class SavedFormRequest(BaseModel):
    id: str | None = Field(default=None, max_length=80)
    form: SavedFormSnapshot
    autosave: bool = False
    telegram_init_data: str | None = Field(default=None, max_length=16000)
    telegram_user_session: str | None = Field(default=None, max_length=1000)


class PurchaseHistoryPdfRequest(BaseModel):
    ref: str = Field(..., min_length=6, max_length=160)


class PurchaseHistoryResendRequest(BaseModel):
    ref: str = Field(..., min_length=6, max_length=160)


class ClientEventRequest(BaseModel):
    event_type: str = Field(..., min_length=3, max_length=80)
    current_step: int | None = Field(default=None, ge=1, le=10)
    form: SavedFormSnapshot | None = None
    extra: dict[str, Any] | None = None
    telegram_init_data: str | None = Field(default=None, max_length=16000)
    telegram_user_session: str | None = Field(default=None, max_length=1000)


class CouponValidateRequest(BaseModel):
    code: str = Field(..., min_length=2, max_length=64)
    price_ils: float = Field(..., gt=0, le=50_000)
    telegram_init_data: str | None = Field(default=None, max_length=16000)
    telegram_user_session: str | None = Field(default=None, max_length=1000)


class AdminCreateCouponRequest(BaseModel):
    code: str | None = Field(default=None, max_length=64)
    discount_type: str = Field(..., max_length=16)
    value: float = Field(..., gt=0, le=50_000)
    max_uses: int | None = Field(default=None, ge=1, le=100_000)
    expires_at: str | None = Field(default=None, max_length=48)
    telegram_user_id: int | None = None
    active: bool = True
    note: str | None = Field(default=None, max_length=240)


class CouponActiveRequest(BaseModel):
    active: bool


class AdminResendPdfRequest(BaseModel):
    ref: str = Field(..., min_length=6, max_length=160)
    telegram_user_id: int | None = None


class MiniAppSessionRequest(BaseModel):
    telegram_init_data: str | None = Field(default=None, max_length=16000)
    telegram_user_session: str | None = Field(default=None, max_length=1000)


class RateLimitOverrideRequest(BaseModel):
    telegram_user_id: int
    expires_at: str | None = Field(default=None, max_length=48)
    bypass: bool = True
    multiplier: float = Field(default=2.0, ge=1.0, le=100.0)
    notes: str | None = Field(default=None, max_length=240)


class AdminBroadcastRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4096)
    limit: int = Field(default=500, ge=1, le=2000)
    dry_run: bool = False


def _build_redemption_dict(tg_user: Any, form: RedeemFormSnapshot | None) -> dict[str, Any]:
    """Persist Mini App user + optional form snapshot when a payment code is redeemed."""
    out: dict[str, Any] = {}
    if tg_user:
        ident = _telegram_log_identity(tg_user)
        out["telegram_user_id"] = ident["telegram_user_id"]
        if ident["username"]:
            out["username"] = ident["username"]
        if ident["first_name"]:
            out["first_name"] = ident["first_name"]
    if form:
        raw = form.model_dump(exclude_none=True)
        for key in ("hebrew_full_name", "english_full_name", "id_number", "expiration_date", "expiry_option"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                out[key] = val.strip()
            elif val is not None and key == "expiry_option":
                s = str(val).strip()
                if s:
                    out[key] = s
    return out


def _index_html_response() -> HTMLResponse:
    """Serve built React SPA when ``dist/index.html`` exists (Docker/Railway); else dev ``index.html``."""
    dist_index = DIST_DIR / "index.html"
    if dist_index.is_file():
        raw = dist_index.read_text(encoding="utf-8")
        return HTMLResponse(content=raw, media_type="text/html; charset=utf-8")
    path = ROOT_DIR / "index.html"
    raw = path.read_text(encoding="utf-8")
    return HTMLResponse(content=raw, media_type="text/html; charset=utf-8")


app = FastAPI(title="PDF Name Editor")
_telegram_webhook_app = None


def _telegram_updates_mode() -> str:
    return os.environ.get("TELEGRAM_BOT_UPDATES_MODE", "polling").strip().lower()


def _telegram_polling_disabled() -> bool:
    return os.environ.get("START_TELEGRAM_BOT_SUBPROCESS", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    )


def _should_enable_telegram_webhook() -> bool:
    mode = _telegram_updates_mode()
    if mode in ("webhook", "auto"):
        return True
    if mode in ("none", "external", "off"):
        return False
    if mode == "polling":
        return _telegram_polling_disabled()
    return _telegram_polling_disabled()


def _telegram_webhook_secret_path(token: str) -> str:
    raw = os.environ.get("TELEGRAM_WEBHOOK_SECRET_PATH", "").strip().strip("/")
    if raw:
        return raw.rsplit("/", 1)[-1]
    return secrets.token_urlsafe(24)


@app.on_event("startup")
async def startup_telegram_webhook() -> None:
    global _telegram_webhook_app
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not _should_enable_telegram_webhook():
        return
    base = effective_public_base_url()
    if not base:
        logger.warning("[telegram:webhook] skipped public_base_url_missing")
        return
    from app.telegram_bot import build_bot_application, configure_bot_application

    tg_app = build_bot_application(token, clear_webhook_on_init=False)
    await tg_app.initialize()
    await tg_app.start()
    await configure_bot_application(tg_app, clear_webhook=False)
    path = _telegram_webhook_secret_path(token)
    webhook_url = f"{base.rstrip('/')}/telegram/webhook/{path}"
    await tg_app.bot.set_webhook(
        url=webhook_url,
        allowed_updates=TelegramUpdate.ALL_TYPES,
        drop_pending_updates=True,
    )
    app.state.telegram_webhook_path = path
    _telegram_webhook_app = tg_app
    logger.info("[telegram:webhook] enabled url=%s", webhook_url)


@app.on_event("shutdown")
async def shutdown_telegram_webhook() -> None:
    global _telegram_webhook_app
    if _telegram_webhook_app is None:
        return
    await _telegram_webhook_app.stop()
    await _telegram_webhook_app.shutdown()
    _telegram_webhook_app = None


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request) -> dict[str, bool]:
    if secret != getattr(app.state, "telegram_webhook_path", None):
        raise HTTPException(status_code=404, detail="not_found")
    if _telegram_webhook_app is None:
        raise HTTPException(status_code=503, detail="telegram_webhook_not_ready")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid_json") from exc
    update = TelegramUpdate.de_json(payload, _telegram_webhook_app.bot)
    await _telegram_webhook_app.process_update(update)
    return {"ok": True}


def _cors_allow_origins() -> list[str]:
    """Comma-separated list in CORS_ORIGINS, or * when unset (Netlify → set your *.netlify.app)."""
    raw = os.environ.get("CORS_ORIGINS", "").strip()
    if not raw:
        return ["*"]
    return [x.strip() for x in raw.split(",") if x.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Pdf-Download-Path", "X-Request-ID"],
)
app.add_middleware(StructuredLoggingMiddleware)


@app.get("/health")
def health() -> dict[str, str]:
    """Process liveness (no database check)."""
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready() -> dict[str, str]:
    """Readiness: verifies database connectivity."""
    try:
        with connect_storage() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        logger.warning("health_ready database check failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "database": str(exc)},
        ) from exc
    return {"status": "ready", "database": "ok"}


def _telegram_user_from_webapp_request(
    request: Request,
    *,
    x_telegram_init_data: str | None = None,
    tg_init_data_query: str | None = None,
    body_init_data: str | None = None,
    tg_user_sess: str | None = None,
    authorization: str | None = None,
):
    auth = authorization or request.headers.get("Authorization") or request.headers.get("authorization")
    user = resolve_telegram_webapp_user(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data_query,
        body_init_data=body_init_data,
        tg_user_sess=tg_user_sess,
        authorization=auth,
    )
    logger.debug(
        "[telegram:session] webapp_user_lookup path=%s method=%s telegram_user_id=%s",
        request.url.path,
        request.method,
        user.id if user else None,
    )
    return user


def _require_mini_app_user(
    request: Request,
    *,
    x_telegram_init_data: str | None = None,
    tg_init_data_query: str | None = None,
    body_init_data: str | None = None,
    tg_user_sess: str | None = None,
    authorization: str | None = None,
):
    user = _telegram_user_from_webapp_request(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data_query,
        body_init_data=body_init_data,
        tg_user_sess=tg_user_sess,
        authorization=authorization,
    )
    if user is None:
        logger.info(
            "[telegram:session] require_mini_app_user denied path=%s method=%s detail=%s",
            request.url.path,
            request.method,
            _TELEGRAM_USER_REQUIRED_DETAIL["code"],
        )
        raise HTTPException(status_code=401, detail=_TELEGRAM_USER_REQUIRED_DETAIL)
    return user


def _telegram_log_identity(tg_user) -> dict[str, Any]:
    if tg_user is None:
        return {"telegram_user_id": None, "username": None, "first_name": None}
    try:
        upsert_from_telegram_user(tg_user, source="mini_app", event_type="identity_seen")
    except Exception:
        logger.exception(
            "[telegram:users] upsert failed telegram_user_id=%s",
            getattr(tg_user, "id", None),
        )
    username = getattr(tg_user, "username", None)
    first_name = getattr(tg_user, "first_name", None)
    if not username or not first_name:
        profile = get_telegram_chat_profile(getattr(tg_user, "id", None))
        username = username or profile.get("username")
        first_name = first_name or profile.get("first_name")
    return {
        "telegram_user_id": tg_user.id,
        "username": username,
        "first_name": first_name,
    }


def _request_meta(request: Request, *, watermark: bool | None = None) -> dict[str, str | bool | None]:
    ua = request.headers.get("user-agent", "")
    auth = (request.headers.get("authorization") or "").lower()
    is_tg = bool(
        request.headers.get("x-telegram-init-data")
        or auth.startswith("tma ")
    )
    return {
        "watermark": watermark,
        "client": "telegram" if is_tg else "web",
        "user_agent": ua[:120] if ua else None,
    }


def _base_price_for_expiry(expiry_option: str | None, fallback: float) -> float:
    key = str(expiry_option or "").strip()
    if key in VALID_TIER_KEYS:
        return float(key)
    return float(fallback)


def _coupon_quote(
    coupon_code: str | None,
    *,
    original_price_ils: float,
    telegram_user_id: int | None,
) -> dict[str, Any]:
    code = normalize_coupon_code(coupon_code or "")
    if not code:
        return {
            "coupon_code": None,
            "discount_ils": 0.0,
            "final_price_ils": round(float(original_price_ils), 2),
        }
    quote = validate_coupon(
        code,
        original_price_ils=original_price_ils,
        telegram_user_id=telegram_user_id,
    )
    if not quote.get("ok"):
        raise HTTPException(status_code=400, detail=quote.get("reason") or "invalid_coupon")
    return {
        "coupon_code": code,
        "discount_ils": float(quote["discount_ils"]),
        "final_price_ils": float(quote["final_price_ils"]),
        "coupon": quote.get("coupon"),
    }


_CLIENT_EVENT_TYPES = {
    "mini_app_opened",
    "mini_app_form_started",
    "mini_app_payment_screen",
    "mini_app_abandoned",
}


def _client_form_meta(form: SavedFormSnapshot | None) -> dict[str, Any] | None:
    if form is None:
        return None
    raw = form.model_dump()
    return {
        "has_full_name": bool((raw.get("fullName") or "").strip()),
        "has_full_name_en": bool((raw.get("fullNameEn") or "").strip()),
        "has_id_number": bool((raw.get("idNumber") or "").strip()),
        "expiry_option": (raw.get("expiryOption") or "").strip() or None,
    }


# Register before /static mount so /static/index.html is HTML, not a mis-typed static file.
@app.get("/")
def index() -> HTMLResponse:
    return _index_html_response()


@app.get("/admin")
def admin_index() -> HTMLResponse:
    return _index_html_response()


@app.get("/admin/{path:path}")
def admin_index_path(path: str) -> HTMLResponse:
    return _index_html_response()


@app.get("/static/index.html")
def static_index() -> HTMLResponse:
    return _index_html_response()


if DIST_ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(DIST_ASSETS_DIR)), name="dist_assets")

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/api/my-saved-forms")
def my_saved_forms(
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Query(default=None),
    tg_user_sess: str | None = Query(default=None),
) -> dict:
    tg_user = _require_mini_app_user(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data,
        tg_user_sess=tg_user_sess,
        authorization=authorization,
    )
    return list_for_user(tg_user.id)


@app.put("/api/my-saved-forms")
def save_my_form(
    payload: SavedFormRequest,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Query(default=None),
) -> dict:
    tg_user = _require_mini_app_user(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data,
        body_init_data=payload.telegram_init_data,
        tg_user_sess=payload.telegram_user_session,
        authorization=authorization,
    )
    ident = _telegram_log_identity(tg_user)
    row = upsert_for_user(
        tg_user.id,
        form=payload.form.model_dump(),
        form_id=payload.id,
    )
    log_event(
        "saved_form_upserted",
        source="mini_app",
        telegram_user_id=ident["telegram_user_id"],
        username=ident["username"],
        first_name=ident["first_name"],
        meta={"saved_form_id": row["id"], "title": row.get("title"), "autosave": payload.autosave},
    )
    return row


@app.delete("/api/my-saved-forms/{form_id}")
def delete_my_form(
    form_id: str,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Query(default=None),
    tg_user_sess: str | None = Query(default=None),
) -> dict[str, bool]:
    tg_user = _require_mini_app_user(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data,
        tg_user_sess=tg_user_sess,
        authorization=authorization,
    )
    ident = _telegram_log_identity(tg_user)
    out = delete_for_user(tg_user.id, form_id)
    log_event(
        "saved_form_deleted",
        source="mini_app",
        telegram_user_id=ident["telegram_user_id"],
        username=ident["username"],
        first_name=ident["first_name"],
        meta={"saved_form_id": form_id, "deleted": out["ok"]},
    )
    return out


@app.post("/api/mini-app/session")
def mini_app_session(
    payload: MiniAppSessionRequest,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Query(default=None),
) -> dict[str, str]:
    """Mint or refresh ``tg_user_sess`` after verifying WebApp initData (or existing valid sess)."""
    tg_user = _telegram_user_from_webapp_request(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data,
        body_init_data=payload.telegram_init_data,
        tg_user_sess=payload.telegram_user_session,
        authorization=authorization,
    )
    if tg_user is None:
        logger.info(
            "[telegram:session] mini_app_session rejected path=%s detail=%s",
            request.url.path,
            _TELEGRAM_USER_REQUIRED_DETAIL["code"],
        )
        raise HTTPException(status_code=401, detail=_TELEGRAM_USER_REQUIRED_DETAIL)
    token = mint_user_tg_sess(tg_user.id)
    if not token:
        logger.error(
            "[telegram:session] mini_app_session mint failed telegram_user_id=%s detail=session_mint_unavailable",
            tg_user.id,
        )
        raise HTTPException(status_code=503, detail="session_mint_unavailable")
    logger.info("[telegram:session] mini_app_session minted telegram_user_id=%s", tg_user.id)
    return {"tg_user_sess": token}


@app.post("/api/client-event")
def client_event(
    payload: ClientEventRequest,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Query(default=None),
) -> dict[str, bool]:
    event_type = payload.event_type.strip()
    if event_type not in _CLIENT_EVENT_TYPES:
        raise HTTPException(status_code=400, detail="invalid_event_type")
    tg_user = _telegram_user_from_webapp_request(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data,
        body_init_data=payload.telegram_init_data,
        tg_user_sess=payload.telegram_user_session,
        authorization=authorization,
    )
    ident = _telegram_log_identity(tg_user)
    meta: dict[str, Any] = {
        **_request_meta(request),
        "current_step": payload.current_step,
    }
    form_meta = _client_form_meta(payload.form)
    if form_meta:
        meta["form"] = form_meta
    if isinstance(payload.extra, dict):
        for k, v in payload.extra.items():
            if isinstance(k, str) and len(k) <= 40 and isinstance(v, (str, int, float, bool, type(None))):
                meta[k] = v
    log_event(
        event_type,
        source="mini_app",
        telegram_user_id=ident["telegram_user_id"],
        username=ident["username"],
        first_name=ident["first_name"],
        meta=meta,
    )
    return {"ok": True}


@app.post("/api/coupons/validate")
def validate_coupon_api(
    payload: CouponValidateRequest,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Query(default=None),
) -> dict[str, Any]:
    tg_user = _telegram_user_from_webapp_request(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data,
        body_init_data=payload.telegram_init_data,
        tg_user_sess=payload.telegram_user_session,
        authorization=authorization,
    )
    quote = validate_coupon(
        payload.code,
        original_price_ils=payload.price_ils,
        telegram_user_id=tg_user.id if tg_user else None,
    )
    return {
        "ok": bool(quote.get("ok")),
        "reason": quote.get("reason"),
        "code": normalize_coupon_code(payload.code),
        "discount_ils": quote.get("discount_ils", 0.0),
        "final_price_ils": quote.get("final_price_ils", payload.price_ils),
    }


@app.get("/api/my-purchase-history")
def my_purchase_history(
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Query(default=None),
    tg_user_sess: str | None = Query(default=None),
) -> dict[str, Any]:
    tg_user = _require_mini_app_user(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data,
        tg_user_sess=tg_user_sess,
        authorization=authorization,
    )
    return _purchase_history_payload(tg_user.id)


@app.post("/api/my-purchase-history/final-pdf")
def purchase_history_final_pdf(
    payload: PurchaseHistoryPdfRequest,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Query(default=None),
    tg_user_sess: str | None = Query(default=None),
) -> Response:
    if maintenance_mode_enabled():
        raise HTTPException(status_code=503, detail="maintenance_mode")
    tg_user = _require_mini_app_user(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data,
        tg_user_sess=tg_user_sess,
        authorization=authorization,
    )
    check_rate_limit("final_pdf", request, tg_user)
    ident = _telegram_log_identity(tg_user)
    ref = payload.ref.strip()
    redemption: dict[str, Any]
    if ref.startswith("redeem:"):
        rest = ref.split(":", 1)[1]
        try:
            eid = int(rest)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_ref") from None
        ev = get_payment_redeem_event_for_user(eid, tg_user.id)
        if ev is None:
            raise HTTPException(status_code=404, detail="purchase_not_found")
        redemption = ev.get("redemption") or {}
    elif ref.startswith("crypto:"):
        oid = ref.split(":", 1)[1].strip()
        if not oid:
            raise HTTPException(status_code=400, detail="invalid_ref")
        order = get_order(oid)
        if (
            order is None
            or order.get("telegram_user_id") != tg_user.id
            or order.get("status") != "paid"
        ):
            raise HTTPException(status_code=404, detail="purchase_not_found")
        redemption = order.get("form") or {}
    else:
        raise HTTPException(status_code=400, detail="invalid_ref")

    snap = _normalize_redemption_for_pdf(redemption)
    if snap is None:
        raise HTTPException(status_code=400, detail="incomplete_form_snapshot")
    pdf_bytes = _pdf_from_form_snapshot(snap)
    if pdf_bytes is None:
        raise HTTPException(status_code=500, detail="could_not_build_pdf")

    dl_token = register_pdf_bytes(pdf_bytes, user_meta=ident)
    headers = {
        "Content-Disposition": f'inline; filename="{OUTPUT_PDF_FILENAME}"',
        "X-Pdf-Download-Path": f"/pdf-download/{dl_token}",
    }
    log_event(
        "pdf_generated",
        source="mini_app",
        telegram_user_id=ident["telegram_user_id"],
        username=ident["username"],
        first_name=ident["first_name"],
        meta={
            **_request_meta(request, watermark=False),
            "payment_status": "paid_final",
            "replay_ref": ref,
            "form": {
                "hebrew_full_name": snap.get("hebrew_full_name"),
                "english_full_name": snap.get("english_full_name"),
                "id_number": snap.get("id_number"),
                "expiration_date": snap.get("expiration_date"),
            },
        },
    )
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@app.post("/api/my-purchase-history/resend-pdf")
def purchase_history_resend_pdf(
    payload: PurchaseHistoryResendRequest,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Query(default=None),
    tg_user_sess: str | None = Query(default=None),
) -> dict[str, bool]:
    if maintenance_mode_enabled():
        raise HTTPException(status_code=503, detail="maintenance_mode")
    tg_user = _require_mini_app_user(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data,
        tg_user_sess=tg_user_sess,
        authorization=authorization,
    )
    check_rate_limit("resend_pdf", request, tg_user)
    ident = _telegram_log_identity(tg_user)
    sent = _resend_purchase_pdf(
        ref=payload.ref.strip(),
        chat_id=tg_user.id,
        telegram_user_id=tg_user.id,
        username=ident["username"],
        first_name=ident["first_name"],
        admin=False,
    )
    if not sent:
        raise HTTPException(status_code=500, detail="telegram_send_failed")
    return {"ok": True}


@app.get("/api/admin/debug")
def admin_debug(
    request: Request,
    tg_init_data: str | None = Query(default=None),
    tg_sess: str | None = Query(default=None),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    authorization: str | None = Header(default=None),
) -> dict:
    """No-auth diagnostic: shows what auth data the server actually received."""
    auth = (authorization or "").strip()
    return {
        "has_x_telegram_init_data_header": bool((x_telegram_init_data or "").strip()),
        "has_tg_init_data_query": bool((tg_init_data or "").strip()),
        "has_tg_sess_query": bool((tg_sess or "").strip()),
        "has_authorization_header": bool(auth),
        "authorization_type": auth.split(" ")[0].lower() if auth else None,
        "bot_token_configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()),
        "admin_secret_source": "explicit" if os.environ.get("ADMIN_API_SECRET", "").strip() else ("derived" if os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() else "none"),
        "admin_secret_configured": bool(effective_admin_secret()),
    }


@app.post("/api/admin/session")
def admin_session(
    request: Request,
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    tg_init_data: str | None = Query(default=None),
    tg_sess: str | None = Query(default=None),
) -> dict[str, str]:
    init_data = (
        x_telegram_init_data
        or request.headers.get("x-telegram-init-data", "")
        or request.headers.get("X-Telegram-Init-Data", "")
        or tg_init_data
        or ""
    ).strip()
    auth = (authorization or request.headers.get("authorization") or "").strip()
    if not init_data and auth.lower().startswith("tma "):
        init_data = auth[4:].strip()
    if init_data:
        user = verify_telegram_init_data(init_data)
        if user.id in admin_ids():
            return {"tg_sess": mint_admin_tg_sess(user.id)}
        raise HTTPException(status_code=403, detail="admin_only")

    sess_user = verify_admin_tg_sess(
        (tg_sess or "").strip(),
        allow_expired_grace_sec=ADMIN_TG_SESS_REFRESH_GRACE_SEC,
    )
    if sess_user:
        return {"tg_sess": mint_admin_tg_sess(sess_user.id)}

    secret = effective_admin_secret()
    if secret and auth == f"Bearer {secret}":
        return {"tg_sess": ""}

    raise HTTPException(status_code=401, detail="admin_auth_required")


@app.get("/api/admin/summary")
def admin_summary(_: AdminIdentity = Depends(require_admin)) -> dict:
    from app.payment_codes_store import codes_summary

    return {
        "activity": activity_summary(),
        "telegram_users": telegram_users_summary(),
        "payment_codes": codes_summary(),
        "coupons": coupons_summary(),
        "control": get_control_state(),
    }


def _event_stats_for_user_ids(user_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not user_ids:
        return {}
    placeholders = ",".join("?" for _ in user_ids)
    try:
        with connect_storage() as conn:
            rows = conn.execute(
                qp(
                    f"""
                    SELECT
                      telegram_user_id,
                      COUNT(*) AS event_count,
                      SUM(CASE WHEN event_type = 'payment_code_redeemed' THEN 1 ELSE 0 END) AS redeem_count,
                      SUM(CASE WHEN event_type = 'pdf_generated' THEN 1 ELSE 0 END) AS pdf_generated_count,
                      SUM(CASE WHEN event_type = 'pdf_downloaded' THEN 1 ELSE 0 END) AS pdf_download_count,
                      SUM(CASE WHEN event_type LIKE 'bot_%' THEN 1 ELSE 0 END) AS bot_events_count
                    FROM events
                    WHERE telegram_user_id IN ({placeholders})
                    GROUP BY telegram_user_id
                    """
                ),
                user_ids,
            ).fetchall()
    except Exception:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        uid = int(row["telegram_user_id"])
        out[uid] = {
            "event_count": int(row["event_count"] or 0),
            "redeem_count": int(row["redeem_count"] or 0),
            "pdf_generated_count": int(row["pdf_generated_count"] or 0),
            "pdf_download_count": int(row["pdf_download_count"] or 0),
            "bot_events_count": int(row["bot_events_count"] or 0),
        }
    return out


@app.get("/api/admin/users")
def admin_users(
    _: AdminIdentity = Depends(require_admin),
    limit: int = Query(default=150, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    data = list_telegram_users(limit=limit, offset=offset, include_disabled=True)
    ids = [int(item["telegram_user_id"]) for item in data.get("items", []) if item.get("telegram_user_id")]
    by_uid = _event_stats_for_user_ids(ids)
    for item in data.get("items", []):
        agg = by_uid.get(int(item["telegram_user_id"]))
        if not agg:
            item.setdefault("event_count", 0)
            item.setdefault("redeem_count", 0)
            item.setdefault("pdf_generated_count", 0)
            item.setdefault("pdf_download_count", 0)
            item.setdefault("bot_events_count", 0)
            continue
        for key in (
            "event_count",
            "redeem_count",
            "pdf_generated_count",
            "pdf_download_count",
            "bot_events_count",
        ):
            item[key] = agg.get(key, item.get(key))
    return data


@app.post("/api/admin/broadcast/send")
def admin_send_broadcast(
    payload: AdminBroadcastRequest,
    identity: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="broadcast_text_required")
    result = send_broadcast(
        text,
        limit=payload.limit,
        dry_run=payload.dry_run,
    )
    log_event(
        "admin_broadcast_sent" if not payload.dry_run else "admin_broadcast_dry_run",
        source="admin",
        telegram_user_id=identity.telegram_user.id if identity.telegram_user else None,
        meta={
            "target_count": result.get("target_count"),
            "sent": result.get("sent"),
            "failed": result.get("failed"),
            "dry_run": payload.dry_run,
            "text_len": len(text),
        },
    )
    return result


@app.get("/api/admin/events")
def admin_events(
    _: AdminIdentity = Depends(require_admin),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    event_type: str | None = Query(default=None),
    telegram_user_id: int | None = Query(default=None),
) -> dict:
    data = list_events(
        limit=limit,
        offset=offset,
        event_type=event_type,
        telegram_user_id=telegram_user_id,
    )
    for item in data.get("items", []):
        uid = item.get("telegram_user_id")
        if not uid or (item.get("username") and item.get("first_name")):
            continue
        profile = get_telegram_chat_profile(uid)
        if not item.get("username") and profile.get("username"):
            item["username"] = profile["username"]
        if not item.get("first_name") and profile.get("first_name"):
            item["first_name"] = profile["first_name"]
    return data


@app.get("/api/admin/payment-codes")
def admin_payment_codes(_: AdminIdentity = Depends(require_admin)) -> dict:
    from app.payment_codes_store import list_codes

    return {"items": list_codes(include_code=True)}


@app.get("/api/admin/coupons")
def admin_coupons(_: AdminIdentity = Depends(require_admin)) -> dict:
    return list_coupons()


@app.post("/api/admin/coupons")
def admin_create_coupon(
    payload: AdminCreateCouponRequest,
    identity: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    try:
        row = create_coupon(
            code=payload.code,
            discount_type=payload.discount_type,
            value=payload.value,
            max_uses=payload.max_uses,
            expires_at=payload.expires_at,
            telegram_user_id=payload.telegram_user_id,
            active=payload.active,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        "coupon_created",
        source="admin_panel",
        telegram_user_id=identity.telegram_user.id if identity.telegram_user else None,
        meta={
            "code": row.get("code"),
            "discount_type": row.get("discount_type"),
            "value": row.get("value"),
            "max_uses": row.get("max_uses"),
            "telegram_user_id": row.get("telegram_user_id"),
        },
    )
    return row


@app.post("/api/admin/coupons/{code}/active")
def admin_set_coupon_active(
    code: str,
    payload: CouponActiveRequest,
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, bool]:
    out = set_coupon_active(code, payload.active)
    log_event(
        "coupon_active_changed",
        source="admin_panel",
        meta={"code": normalize_coupon_code(code), "active": payload.active},
    )
    return out


@app.post("/api/admin/resend-pdf")
def admin_resend_pdf(
    payload: AdminResendPdfRequest,
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, bool]:
    sent = _resend_purchase_pdf(
        ref=payload.ref.strip(),
        chat_id=payload.telegram_user_id,
        telegram_user_id=payload.telegram_user_id,
        username=None,
        first_name=None,
        admin=True,
    )
    if not sent:
        raise HTTPException(status_code=500, detail="telegram_send_failed")
    return {"ok": True}


@app.post("/api/admin/codes/issue")
def admin_issue_payment_codes(
    payload: AdminIssueCodesRequest,
    identity: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    from app.payment_code_meta import MAX_BULK_PER_KEY, MAX_BULK_TOTAL, meta_for_issue_key
    from app.payment_codes_store import issue_new_code

    raw = payload.bulk or {}
    counts: dict[str, int] = {}
    for k_raw, v_raw in raw.items():
        key = str(k_raw).strip()
        norm_key = "global" if key.lower() == "global" else key
        try:
            n = int(v_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"invalid_count:{key}") from None
        if n < 0:
            raise HTTPException(status_code=400, detail="negative_count")
        if n > MAX_BULK_PER_KEY:
            raise HTTPException(
                status_code=400,
                detail=f"max_{MAX_BULK_PER_KEY}_per_type",
            )
        try:
            meta_for_issue_key(norm_key)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown_issue_key:{key}") from None
        if n:
            counts[norm_key] = counts.get(norm_key, 0) + n

    if not counts:
        if not raw:
            counts = {"global": 1}
        else:
            raise HTTPException(status_code=400, detail="empty_bulk")

    total = sum(counts.values())
    if total > MAX_BULK_TOTAL:
        raise HTTPException(status_code=400, detail=f"max_total_{MAX_BULK_TOTAL}")

    items: list[dict[str, Any]] = []
    for key, n in counts.items():
        meta = meta_for_issue_key(key)
        for _ in range(n):
            code = issue_new_code(meta=meta)
            items.append(
                {
                    "code": code,
                    "issue_scope": meta.get("issue_scope"),
                    "issue_label": meta.get("issue_label"),
                    "expiry_option": meta.get("expiry_option"),
                }
            )

    tg_id = identity.telegram_user.id if identity.telegram_user else None
    log_event(
        "payment_codes_bulk_issued",
        source="admin_panel",
        telegram_user_id=tg_id,
        meta={"counts": counts, "total": len(items)},
    )
    return {"items": items, "counts": counts}


@app.get("/api/admin/control")
def admin_control(_: AdminIdentity = Depends(require_admin)) -> dict:
    return get_control_state()


@app.post("/api/admin/maintenance")
def admin_maintenance(payload: MaintenanceModeRequest, _: AdminIdentity = Depends(require_admin)) -> dict:
    state = set_maintenance_mode(payload.enabled)
    log_event("maintenance_changed", source="admin", meta={"enabled": payload.enabled})
    return state


@app.get("/api/admin/rate-limit-overrides")
def admin_rate_limit_overrides(_: AdminIdentity = Depends(require_admin)) -> dict:
    return list_overrides()


@app.post("/api/admin/rate-limit-overrides")
def admin_upsert_rate_limit_override(
    payload: RateLimitOverrideRequest,
    _: AdminIdentity = Depends(require_admin),
) -> dict:
    row = upsert_override(
        telegram_user_id=payload.telegram_user_id,
        expires_at=payload.expires_at,
        bypass=payload.bypass,
        multiplier=payload.multiplier,
        notes=payload.notes,
    )
    log_event(
        "rate_limit_override_saved",
        source="admin",
        telegram_user_id=payload.telegram_user_id,
        meta=row,
    )
    return row


@app.delete("/api/admin/rate-limit-overrides/{telegram_user_id}")
def admin_delete_rate_limit_override(
    telegram_user_id: int,
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, bool]:
    out = delete_override(telegram_user_id)
    log_event(
        "rate_limit_override_deleted",
        source="admin",
        telegram_user_id=telegram_user_id,
    )
    return out


def _pdf_from_form_snapshot(form: RedeemFormSnapshot | dict[str, Any] | None) -> bytes | None:
    if form is None:
        return None
    raw = form if isinstance(form, dict) else form.model_dump(exclude_none=True)
    required = ("hebrew_full_name", "english_full_name", "id_number", "expiration_date")
    if any(not str(raw.get(k, "")).strip() for k in required):
        return None
    return replace_fields(
        hebrew_full_name=str(raw["hebrew_full_name"]).strip(),
        english_full_name=str(raw["english_full_name"]).strip(),
        id_number=str(raw["id_number"]).strip(),
        expiration_date=str(raw["expiration_date"]).strip(),
        watermark=False,
    )


def _normalize_redemption_for_pdf(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip redemption / stored order form to fields required for final PDF."""
    if not raw:
        return None
    he = str(raw.get("hebrew_full_name") or "").strip()
    en = str(raw.get("english_full_name") or "").strip()
    idn = str(raw.get("id_number") or "").strip()
    exp = str(raw.get("expiration_date") or "").strip()
    if not (he and en and idn and exp):
        return None
    out: dict[str, Any] = {
        "hebrew_full_name": he,
        "english_full_name": en,
        "id_number": idn,
        "expiration_date": exp,
    }
    eo = raw.get("expiry_option")
    if eo is not None and str(eo).strip():
        out["expiry_option"] = str(eo).strip()
    return out


def _prefill_dict_from_server_form(red: dict[str, Any]) -> dict[str, str]:
    """Mini App ``formData`` keys from redemption/order form snapshot."""
    return {
        "fullName": str(red.get("hebrew_full_name") or "").strip(),
        "fullNameEn": str(red.get("english_full_name") or "").strip(),
        "idNumber": str(red.get("id_number") or "").strip(),
        "expiryOption": str(red.get("expiry_option") or "").strip(),
        "birthDate": "",
        "idIssueDate": "",
    }


def _purchase_history_payload(telegram_user_id: int) -> dict[str, Any]:
    redeems = list_payment_redeems_for_user(telegram_user_id, limit=40)
    cryptos = list_paid_orders_for_user(telegram_user_id, limit=40)
    items: list[dict[str, Any]] = []
    for ev in redeems:
        red = ev.get("redemption") or {}
        snap = _normalize_redemption_for_pdf(red)
        title = (red.get("hebrew_full_name") or "").strip() or "פטור מתור"
        sub = (red.get("expiration_date") or "").strip()
        items.append(
            {
                "ref": f"redeem:{ev['id']}",
                "kind": "withdraw_code",
                "ts": ev["ts"],
                "title": title[:120],
                "subtitle": sub[:120],
                "downloadable": snap is not None,
                "prefill": _prefill_dict_from_server_form(red),
            }
        )
    for co in cryptos:
        form = co.get("form") or {}
        snap = _normalize_redemption_for_pdf(form)
        title = (form.get("hebrew_full_name") or "").strip() or "פטור מתור"
        po = co.get("price_ils")
        eo = co.get("expiry_option")
        parts: list[str] = []
        if po is not None:
            parts.append(f"₪{int(po)}")
        if eo:
            parts.append(EXPIRY_TIER_LABELS.get(str(eo), str(eo)))
        subtitle = " · ".join(parts) if parts else ""
        items.append(
            {
                "ref": f"crypto:{co['order_id']}",
                "kind": "crypto",
                "ts": co["ts"] or "",
                "title": title[:120],
                "subtitle": subtitle[:120],
                "downloadable": snap is not None,
                "prefill": _prefill_dict_from_server_form(form),
            }
        )
    items.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    return {"items": items[:50]}


def _purchase_snapshot_from_ref(
    ref: str,
    *,
    telegram_user_id: int | None,
    admin: bool = False,
) -> tuple[dict[str, Any], int | None, str | None, str | None]:
    def _int_or_none(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    if ref.startswith("redeem:"):
        rest = ref.split(":", 1)[1]
        try:
            eid = int(rest)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_ref") from None
        if admin:
            ev = get_event(eid)
            if ev is None or ev.get("event_type") != "payment_code_redeemed":
                raise HTTPException(status_code=404, detail="purchase_not_found")
            meta = ev.get("meta") or {}
            redemption = meta.get("redemption") if isinstance(meta.get("redemption"), dict) else {}
            owner = ev.get("telegram_user_id") or redemption.get("telegram_user_id") or telegram_user_id
            username = ev.get("username") or redemption.get("username")
            first_name = ev.get("first_name") or redemption.get("first_name")
        else:
            if telegram_user_id is None:
                raise HTTPException(status_code=401, detail="telegram_user_required")
            ev = get_payment_redeem_event_for_user(eid, telegram_user_id)
            if ev is None:
                raise HTTPException(status_code=404, detail="purchase_not_found")
            redemption = ev.get("redemption") or {}
            owner = telegram_user_id
            username = redemption.get("username")
            first_name = redemption.get("first_name")
        snap = _normalize_redemption_for_pdf(redemption)
        if snap is None:
            raise HTTPException(status_code=400, detail="incomplete_form_snapshot")
        return snap, _int_or_none(owner), username, first_name

    if ref.startswith("crypto:"):
        oid = ref.split(":", 1)[1].strip()
        if not oid:
            raise HTTPException(status_code=400, detail="invalid_ref")
        order = get_order(oid)
        if order is None or order.get("status") != "paid":
            raise HTTPException(status_code=404, detail="purchase_not_found")
        if not admin and order.get("telegram_user_id") != telegram_user_id:
            raise HTTPException(status_code=404, detail="purchase_not_found")
        snap = _normalize_redemption_for_pdf(order.get("form") or {})
        if snap is None:
            raise HTTPException(status_code=400, detail="incomplete_form_snapshot")
        owner = order.get("telegram_user_id") or telegram_user_id
        return (
            snap,
            _int_or_none(owner),
            order.get("username"),
            order.get("first_name"),
        )

    raise HTTPException(status_code=400, detail="invalid_ref")


def _resend_purchase_pdf(
    *,
    ref: str,
    chat_id: int | None,
    telegram_user_id: int | None,
    username: str | None,
    first_name: str | None,
    admin: bool,
) -> bool:
    snap, owner_id, snap_username, snap_first_name = _purchase_snapshot_from_ref(
        ref,
        telegram_user_id=telegram_user_id,
        admin=admin,
    )
    target_chat_id = chat_id or owner_id
    pdf_bytes = _pdf_from_form_snapshot(snap)
    if pdf_bytes is None:
        raise HTTPException(status_code=500, detail="could_not_build_pdf")
    return _send_final_pdf_to_telegram(
        chat_id=target_chat_id,
        pdf_bytes=pdf_bytes,
        event_source="admin_panel" if admin else "mini_app",
        telegram_user_id=owner_id or telegram_user_id,
        username=username or snap_username,
        first_name=first_name or snap_first_name,
        meta={"ref": ref, "reason": "manual_resend" if admin else "user_resend"},
    )


def _send_final_pdf_to_telegram(
    *,
    chat_id: int | None,
    pdf_bytes: bytes | None,
    event_source: str,
    telegram_user_id: int | None,
    username: str | None,
    first_name: str | None,
    meta: dict[str, Any],
) -> bool:
    logger.debug(
        "[telegram:delivery] final_pdf pipeline start source=%s chat_id=%s telegram_user_id=%s pdf_bytes=%s meta_keys=%s",
        event_source,
        chat_id,
        telegram_user_id,
        len(pdf_bytes) if pdf_bytes else None,
        sorted(meta.keys()),
    )
    if not chat_id:
        log_event(
            "telegram_delivery_failed",
            source=event_source,
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            meta={**meta, "error": "no_chat_id"},
        )
        logger.warning(
            "[telegram:delivery] final_pdf skipped reason=no_chat_id source=%s telegram_user_id=%s meta=%s",
            event_source,
            telegram_user_id,
            meta,
        )
        return False
    if not pdf_bytes:
        log_event(
            "telegram_delivery_failed",
            source=event_source,
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            meta={**meta, "error": "pdf_bytes_is_none"},
        )
        logger.warning(
            "[telegram:delivery] final_pdf skipped reason=pdf_bytes_is_none source=%s chat_id=%s meta=%s",
            event_source,
            chat_id,
            meta,
        )
        return False

    caption = "הקובץ הסופי נשמר כאן בצ׳אט כדי שלא יאבד."

    # Multipart first: upload bytes directly from this worker — no reliance on GET /pdf-download
    # being served by the same instance that registered the SQLite token (multi-worker / LB issues).
    ok, err = send_telegram_document(
        chat_id,
        pdf_bytes,
        filename=OUTPUT_PDF_FILENAME,
        caption=caption,
    )
    if ok:
        log_event(
            "telegram_final_pdf_sent",
            source=event_source,
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            meta={**meta, "delivery": "multipart"},
        )
        logger.info(
            "[telegram:delivery] final_pdf sent source=%s chat_id=%s telegram_user_id=%s mode=multipart",
            event_source,
            chat_id,
            telegram_user_id,
        )
        return True

    logger.warning(
        "[telegram:delivery] final_pdf multipart failed source=%s chat_id=%s telegram_user_id=%s err=%s",
        event_source,
        chat_id,
        telegram_user_id,
        err,
    )

    url_err = err
    base_url = effective_public_base_url()
    if base_url:
        try:
            dl_token = register_pdf_bytes(
                pdf_bytes,
                user_meta={
                    "telegram_user_id": telegram_user_id,
                    "username": username,
                    "first_name": first_name,
                },
            )
            pdf_url = f"{base_url}/pdf-download/{dl_token}"
            ok, err = send_telegram_document_url(chat_id, pdf_url, caption=caption)
            if not ok:
                logger.warning(
                    "[telegram:delivery] final_pdf url fallback failed chat_id=%s telegram_user_id=%s dl_token_prefix=%s err=%s",
                    chat_id,
                    telegram_user_id,
                    dl_token[:12],
                    err,
                )
            else:
                log_event(
                    "telegram_final_pdf_sent",
                    source=event_source,
                    telegram_user_id=telegram_user_id,
                    username=username,
                    first_name=first_name,
                    meta={**meta, "delivery": "url"},
                )
                logger.info(
                    "[telegram:delivery] final_pdf sent source=%s chat_id=%s telegram_user_id=%s mode=url",
                    event_source,
                    chat_id,
                    telegram_user_id,
                )
                return True
        except Exception as exc:
            err = f"url_method_exception: {exc}"
            logger.exception(
                "[telegram:delivery] final_pdf url_method_exception chat_id=%s telegram_user_id=%s",
                chat_id,
                telegram_user_id,
            )
    else:
        err = "public_base_url_not_configured"
        logger.warning(
            "[telegram:delivery] final_pdf url fallback unavailable: WEB_APP_URL/public base not set source=%s chat_id=%s",
            event_source,
            chat_id,
        )

    err = f"multipart={url_err} | url_or_fallback={err}"

    log_event(
        "telegram_delivery_failed",
        source=event_source,
        telegram_user_id=telegram_user_id,
        username=username,
        first_name=first_name,
        meta={**meta, "error": err},
    )
    logger.warning(
        "[telegram:delivery] final_pdf failed source=%s telegram_user_id=%s err=%s",
        event_source,
        telegram_user_id,
        err,
    )
    return False


def _owner_telegram_id_for_redeem(ident: dict[str, Any], redemption: dict[str, Any] | None) -> int | None:
    """Prefer ``ident``; fallback to ``redemption.telegram_user_id`` (session without initData)."""
    raw = ident.get("telegram_user_id")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    if isinstance(redemption, dict):
        raw = redemption.get("telegram_user_id")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
    return None


def _resolve_owner_for_redeem(
    ident: dict[str, Any],
    redemption: dict[str, Any],
    telegram_user_session: str | None,
) -> int | None:
    """Same as ``_owner_telegram_id_for_redeem`` plus bot minted ``tg_user_sess`` when initData is empty."""
    uid = _owner_telegram_id_for_redeem(ident, redemption)
    if uid is not None:
        return uid
    sess = (telegram_user_session or "").strip()
    if not sess:
        return None
    u = verify_user_tg_sess(sess)
    return u.id if u else None


def _redeem_payment_code_deliver(
    *,
    chat_id: int | None,
    ident: dict[str, Any],
    norm: str,
    code_hint: str | None,
    form_dict: dict[str, Any] | None,
    redemption: dict[str, Any] | None,
    request_meta: dict[str, Any],
    tg_user_resolved: bool,
    already_pdf_sent: bool,
    telegram_user_session: str | None = None,
) -> None:
    """Send Telegram confirmation/PDF after redeem (purchase is logged synchronously on redeem)."""
    owner_id = _resolve_owner_for_redeem(ident, redemption, telegram_user_session)
    un = ident.get("username")
    fn = ident.get("first_name")
    telegram_pdf_sent = already_pdf_sent
    effective_chat_id = chat_id or owner_id
    logger.debug(
        "[purchase:redeem] deliver start code_last4=%s owner_id=%s notify_chat_id=%s effective_chat_id=%s "
        "tg_user_resolved=%s already_pdf_sent=%s",
        code_hint,
        owner_id,
        chat_id,
        effective_chat_id,
        tg_user_resolved,
        already_pdf_sent,
    )

    try:
        if not effective_chat_id:
            log_event(
                "telegram_notify_skipped_no_user",
                source="mini_app",
                telegram_user_id=owner_id,
                meta={
                    "code_last4": code_hint,
                    "reason": "missing_chat_and_owner",
                },
            )
            logger.info(
                "[purchase:redeem] telegram notify skipped reason=missing_chat_and_owner code_last4=%s owner_id=%s",
                code_hint,
                owner_id,
            )
            return

        pdf_bytes: bytes | None = None
        if not already_pdf_sent:
            try:
                if form_dict:
                    pdf_bytes = _pdf_from_form_snapshot(form_dict)
                if pdf_bytes is None and redemption:
                    pdf_bytes = _pdf_from_form_snapshot(redemption)
                if pdf_bytes is None:
                    log_event(
                        "telegram_pdf_skipped_no_form_data",
                        source="mini_app",
                        telegram_user_id=owner_id,
                        username=un,
                        first_name=fn,
                        meta={
                            "code_last4": code_hint,
                            "form_received": bool(form_dict),
                            "form_fields": form_dict or {},
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "telegram_delivery_failed",
                    source="mini_app",
                    telegram_user_id=owner_id,
                    username=un,
                    first_name=fn,
                    meta={
                        "code_last4": code_hint,
                        "error": str(exc),
                        "exception_type": type(exc).__name__,
                    },
                )

        show_pdf_line = already_pdf_sent or bool(pdf_bytes)
        approval_text = (
            "✅ <b>תשלום אושר במערכת</b>\n\n"
            + (
                "📎 קובץ PDF סופי נשלח גם כאן בצ׳אט."
                if show_pdf_line
                else "ניתן להוריד את הקובץ הסופי מהמיני־אפליקציה."
            )
        )
        logger.debug(
            "[purchase:redeem] sending confirmation DM chat_id=%s code_last4=%s show_pdf_line=%s",
            effective_chat_id,
            code_hint,
            show_pdf_line,
        )
        ok_msg, err_msg = send_telegram_message(effective_chat_id, approval_text)
        if not ok_msg:
            log_event(
                "telegram_delivery_failed",
                source="mini_app",
                telegram_user_id=owner_id,
                username=un,
                first_name=fn,
                meta={
                    "code_last4": code_hint,
                    "kind": "redeem_confirmation",
                    "error": err_msg,
                },
            )

        if not already_pdf_sent and pdf_bytes:
            try:
                if _send_final_pdf_to_telegram(
                    chat_id=effective_chat_id,
                    pdf_bytes=pdf_bytes,
                    event_source="mini_app",
                    telegram_user_id=owner_id,
                    username=un,
                    first_name=fn,
                    meta={"code_last4": code_hint, "reason": "payment_code_redeemed"},
                ):
                    mark_code_telegram_pdf_sent(norm)
                    telegram_pdf_sent = True
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "telegram_delivery_failed",
                    source="mini_app",
                    telegram_user_id=owner_id,
                    username=un,
                    first_name=fn,
                    meta={
                        "code_last4": code_hint,
                        "error": str(exc),
                        "exception_type": type(exc).__name__,
                    },
                )
    except Exception:
        logger.exception("redeem_payment_code_deliver failed")


@app.post("/api/crypto/create-invoice")
async def crypto_create_invoice(
    payload: CreateCryptoInvoiceRequest,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Query(default=None),
) -> dict:
    """Create a NOWPayments invoice and return the hosted payment URL."""
    base_url = effective_public_base_url()
    if not base_url:
        logger.warning("[purchase:crypto] create_invoice blocked detail=WEB_APP_URL_not_configured")
        raise HTTPException(status_code=503, detail="WEB_APP_URL not configured")

    # Require server-verified Telegram identity so crypto_orders.telegram_user_id and IPN-issued
    # codes always carry purchaser_telegram_user_id for redeem-time DMs. Unauthenticated
    # payload.telegram_user_id alone is not accepted (spoofable). DM cannot fire without a
    # stored chat id; old orders/codes created with NULL id need manual support or repurchase.
    tg_user = _require_mini_app_user(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data,
        body_init_data=payload.telegram_init_data,
        tg_user_sess=payload.telegram_user_session,
        authorization=authorization,
    )
    check_rate_limit("create_invoice", request, tg_user)
    ident = _telegram_log_identity(tg_user)

    # Identity is always from verified initData or bot-signed tg_user_sess (never raw body id).
    real_user_id = ident["telegram_user_id"]
    real_username = ident["username"] or payload.username
    real_first_name = ident["first_name"] or payload.first_name
    form_snapshot = _build_redemption_dict(tg_user, payload.form)
    original_price = _base_price_for_expiry(payload.expiry_option, payload.price_ils)
    coupon = _coupon_quote(
        payload.coupon_code,
        original_price_ils=original_price,
        telegram_user_id=real_user_id,
    )
    final_price = max(1.0, float(coupon["final_price_ils"]))
    order_id = str(uuid.uuid4())
    logger.debug(
        "[purchase:crypto] create_invoice start order_id=%s price_ils=%s expiry_option=%s",
        order_id,
        final_price,
        payload.expiry_option,
    )

    description = f"פטור מתור · {payload.expiry_option or ''} · ₪{int(final_price)}"

    try:
        invoice = await nowpayments_create_invoice(
            price_amount=final_price,
            price_currency="ils",
            order_id=order_id,
            order_description=description,
            ipn_callback_url=f"{base_url}/api/crypto/ipn",
            success_url=f"{base_url}/static/index.html?crypto_order={order_id}",
            cancel_url=f"{base_url}/static/index.html",
        )
    except httpx.HTTPStatusError as exc:
        body_preview = (exc.response.text or "")[:4000]
        logger.error(
            "NOWPayments HTTP %s: %s",
            exc.response.status_code,
            body_preview or "(empty body)",
        )
        raise HTTPException(
            status_code=502,
            detail="NOWPayments request failed; see server logs for details.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    invoice_url = invoice.get("invoice_url") or invoice.get("invoiceUrl")
    if not invoice_url:
        raise HTTPException(status_code=502, detail="NOWPayments did not return invoice_url")

    create_order(
        order_id=order_id,
        telegram_user_id=real_user_id,
        username=real_username,
        first_name=real_first_name,
        price_ils=final_price,
        original_price_ils=original_price,
        discount_ils=float(coupon["discount_ils"]),
        coupon_code=coupon["coupon_code"],
        expiry_option=payload.expiry_option,
        invoice_url=invoice_url,
        form=form_snapshot or None,
    )
    logger.info(
        "[purchase:crypto] order persisted order_id=%s telegram_user_id=%s price_ils=%s",
        order_id,
        real_user_id,
        payload.price_ils,
    )
    log_event(
        "crypto_invoice_created",
        source="mini_app",
        telegram_user_id=real_user_id,
        username=real_username,
        first_name=real_first_name,
        meta={
            "order_id": order_id,
            "price_ils": final_price,
            "original_price_ils": original_price,
            "discount_ils": coupon["discount_ils"],
            "coupon_code": coupon["coupon_code"],
            "expiry_option": payload.expiry_option,
            "form": form_snapshot,
        },
    )
    return {"order_id": order_id, "invoice_url": invoice_url}


@app.post("/api/manual-payment-request")
def manual_payment_request(
    payload: ManualPaymentRequest,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Query(default=None),
) -> dict[str, bool]:
    """Log a payment-button click and notify the owner before routing the user to Telegram."""
    tg_user = _telegram_user_from_webapp_request(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data,
        body_init_data=payload.telegram_init_data,
        tg_user_sess=payload.telegram_user_session,
        authorization=authorization,
    )
    check_rate_limit("manual_payment_request", request, tg_user)
    ident = _telegram_log_identity(tg_user)
    form_snapshot = _build_redemption_dict(tg_user, payload.form)
    original_price = _base_price_for_expiry(payload.expiry_option, payload.price_ils)
    final_price = float(payload.final_price_ils or original_price)
    method = re.sub(r"[^a-zA-Z0-9_-]", "", payload.method.strip().lower()) or "unknown"
    log_event(
        "manual_payment_requested",
        source="mini_app",
        telegram_user_id=ident["telegram_user_id"],
        username=ident["username"],
        first_name=ident["first_name"],
        meta={
            **_request_meta(request),
            "method": method,
            "price_ils": original_price,
            "final_price_ils": final_price,
            "discount_ils": float(payload.discount_ils or 0),
            "coupon_code": normalize_coupon_code(payload.coupon_code or "") or None,
            "expiry_option": payload.expiry_option,
            "form": form_snapshot,
        },
    )
    return {"ok": True}


@app.get("/api/crypto/order-status")
def crypto_order_status(order_id: str = Query(..., min_length=4)) -> dict:
    """Poll order payment status (used by Mini App after opening invoice URL).

    Unauthenticated by design: ``order_id`` must be an unguessable UUID from
    ``/api/crypto/create-invoice``. Never use short or sequential identifiers here.
    """
    order = get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    return {
        "order_id": order["order_id"],
        "status": order["status"],
        "paid": order["status"] == "paid",
    }


@app.post("/api/crypto/ipn")
async def crypto_ipn(request: Request) -> JSONResponse:
    """NOWPayments IPN webhook — validates signature, issues payment code, notifies user."""
    req_id = request.headers.get("x-request-id") or request.headers.get("X-Request-ID")
    raw_body = await request.body()
    sig = request.headers.get("x-nowpayments-sig", "")

    logger.debug(
        "[purchase:crypto_ipn] received request_id=%s body_len=%s has_sig=%s",
        req_id,
        len(raw_body or b""),
        bool(sig),
    )

    if not verify_ipn_signature(raw_body, sig):
        logger.warning(
            "[purchase:crypto_ipn] rejected invalid_ipn_signature request_id=%s",
            req_id,
        )
        raise HTTPException(status_code=400, detail="invalid_ipn_signature")

    try:
        data: dict[str, Any] = await request.json()
    except Exception:
        logger.warning("[purchase:crypto_ipn] rejected invalid_json request_id=%s", req_id, exc_info=True)
        raise HTTPException(status_code=400, detail="invalid_json")

    payment_status = (data.get("payment_status") or "").lower()
    order_id = data.get("order_id", "")

    logger.debug(
        "[purchase:crypto_ipn] parsed order_id=%s payment_status=%s request_id=%s",
        order_id or "(empty)",
        payment_status or "(empty)",
        req_id,
    )

    # Only act on fully settled payments (not partially_paid — avoids granting before full receipt).
    if payment_status not in {"finished", "confirmed"}:
        logger.info(
            "[purchase:crypto_ipn] ignored non-terminal payment_status=%s order_id=%s request_id=%s",
            payment_status,
            order_id or "(empty)",
            req_id,
        )
        return JSONResponse({"ok": True, "ignored": True, "payment_status": payment_status})

    if not order_id:
        logger.info("[purchase:crypto_ipn] ignored no_order_id request_id=%s", req_id)
        return JSONResponse({"ok": True, "ignored": True, "reason": "no_order_id"})

    order = get_order(order_id)
    if order is None:
        logger.warning(
            "[purchase:crypto_ipn] ignored order_not_found order_id=%s request_id=%s",
            order_id,
            req_id,
        )
        return JSONResponse({"ok": True, "ignored": True, "reason": "order_not_found"})
    if order.get("status") == "paid":
        logger.info(
            "[purchase:crypto_ipn] duplicate ipn order already_paid order_id=%s request_id=%s",
            order_id,
            req_id,
        )
        return JSONResponse({"ok": True, "updated": False, "reason": "already_paid"})

    # Issue a one-time payment code for this order (persist purchaser id for redeem notify fallback)
    code_meta: dict[str, Any] = {"source": "crypto", "order_id": order_id}
    tu = order.get("telegram_user_id")
    if tu is not None:
        try:
            code_meta["purchaser_telegram_user_id"] = int(tu)
        except (TypeError, ValueError):
            pass
    else:
        logger.warning(
            "[purchase:crypto_ipn] code issued without purchaser_telegram_user_id order_id=%s "
            "(order row has no telegram_user_id; redeem DM cannot target a chat)",
            order_id,
        )
    code = issue_new_code(meta=code_meta)
    updated = mark_paid(order_id=order_id, payment_code=code, ipn_payload=data)
    if updated and order.get("coupon_code"):
        record_coupon_use(str(order.get("coupon_code")))

    logger.info(
        "[purchase:crypto_ipn] payment_confirmed order_id=%s updated=%s telegram_user_id=%s request_id=%s",
        order_id,
        updated,
        order.get("telegram_user_id"),
        req_id,
    )

    log_event(
        "crypto_payment_confirmed",
        source="nowpayments_ipn",
        telegram_user_id=order.get("telegram_user_id"),
        username=order.get("username"),
        first_name=order.get("first_name"),
        meta={
            "order_id": order_id,
            "payment_status": payment_status,
            "price_ils": order.get("price_ils"),
            "original_price_ils": order.get("original_price_ils"),
            "discount_ils": order.get("discount_ils"),
            "coupon_code": order.get("coupon_code"),
            "already_processed": not updated,
        },
    )

    # Notify the user via Telegram if we know their chat id
    tg_user_id = order.get("telegram_user_id")
    if tg_user_id and updated:
        logger.debug(
            "[purchase:crypto_ipn] starting telegram notify order_id=%s telegram_user_id=%s",
            order_id,
            tg_user_id,
        )
        pdf_sent = False
        pdf_bytes = None
        try:
            pdf_bytes = _pdf_from_form_snapshot(order.get("form"))
        except Exception as exc:  # noqa: BLE001
            log_event(
                "telegram_delivery_failed",
                source="nowpayments_ipn",
                telegram_user_id=tg_user_id,
                username=order.get("username"),
                first_name=order.get("first_name"),
                meta={"order_id": order_id, "error": str(exc)},
            )
            logger.exception(
                "[purchase:crypto_ipn] pdf_from_form_snapshot failed order_id=%s telegram_user_id=%s",
                order_id,
                tg_user_id,
            )
        notice = (
            "✅ <b>התשלום התקבל ואושר</b>\n\n"
            f"🔑 קוד האישור שלכם: <code>{code}</code>\n\n"
            + (
                "📎 קובץ PDF סופי נשלח גם כאן בצ׳אט."
                if pdf_bytes
                else "יש לחזור למיני־אפליקציה, להזין את הקוד בשדה אישור התשלום ולהוריד את הקובץ."
            )
        )
        ok_msg, err_msg = send_telegram_message(tg_user_id, notice)
        if not ok_msg:
            log_event(
                "telegram_delivery_failed",
                source="nowpayments_ipn",
                telegram_user_id=tg_user_id,
                username=order.get("username"),
                first_name=order.get("first_name"),
                meta={"order_id": order_id, "kind": "crypto_payment_notice", "error": err_msg},
            )
            logger.warning(
                "[purchase:crypto_ipn] sendMessage failed order_id=%s telegram_user_id=%s err=%s",
                order_id,
                tg_user_id,
                err_msg,
            )
        else:
            logger.info(
                "[purchase:crypto_ipn] payment notice sent order_id=%s telegram_user_id=%s has_pdf=%s",
                order_id,
                tg_user_id,
                bool(pdf_bytes),
            )
        if pdf_bytes:
            try:
                pdf_sent = _send_final_pdf_to_telegram(
                    chat_id=tg_user_id,
                    pdf_bytes=pdf_bytes,
                    event_source="nowpayments_ipn",
                    telegram_user_id=tg_user_id,
                    username=order.get("username"),
                    first_name=order.get("first_name"),
                    meta={"order_id": order_id, "reason": "crypto_payment_confirmed"},
                )
                if pdf_sent:
                    mark_pdf_sent(order_id)
                    mark_code_telegram_pdf_sent(code)
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "telegram_delivery_failed",
                    source="nowpayments_ipn",
                    telegram_user_id=tg_user_id,
                    username=order.get("username"),
                    first_name=order.get("first_name"),
                    meta={"order_id": order_id, "error": str(exc)},
                )
                logger.exception(
                    "[purchase:crypto_ipn] final_pdf pipeline exception order_id=%s telegram_user_id=%s",
                    order_id,
                    tg_user_id,
                )

    elif not tg_user_id and updated:
        logger.warning(
            "[purchase:crypto_ipn] no telegram_user_id on order; skipping DM order_id=%s request_id=%s",
            order_id,
            req_id,
        )
    elif tg_user_id and not updated:
        logger.debug(
            "[purchase:crypto_ipn] skip telegram notify (mark_paid no-op) order_id=%s telegram_user_id=%s",
            order_id,
            tg_user_id,
        )

    return JSONResponse({"ok": True, "updated": updated})


@app.get("/api/admin/crypto-orders")
def admin_crypto_orders(
    _: AdminIdentity = Depends(require_admin),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    include_ipn: bool = Query(default=False),
) -> dict:
    if include_ipn:
        return list_orders_for_admin(limit=limit, offset=offset)
    return list_orders(limit=limit, offset=offset)


@app.post("/redeem-payment-code")
def redeem_payment_code(
    payload: RedeemPaymentCodeRequest,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Query(default=None),
) -> dict[str, bool | str]:
    """Validate and consume a one-time code issued via the Telegram bot."""
    tg_user = _telegram_user_from_webapp_request(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data,
        body_init_data=payload.telegram_init_data,
        tg_user_sess=payload.telegram_user_session,
        authorization=authorization,
    )
    logger.info(
        "redeem_payment_code incoming tg_user_id=%s has_body_init=%s",
        tg_user.id if tg_user else None,
        bool((payload.telegram_init_data or "").strip()),
    )
    check_rate_limit("redeem", request, tg_user)
    ident = _telegram_log_identity(tg_user)
    norm = normalize_code(payload.code)
    code_hint = norm[-4:].upper() if len(norm) >= 4 else None

    form_expiry_opt = payload.form.expiry_option if payload.form else None
    redemption = _build_redemption_dict(tg_user, payload.form)
    owner_id = _resolve_owner_for_redeem(ident, redemption, payload.telegram_user_session)
    if owner_id is not None and not redemption.get("telegram_user_id"):
        redemption = {**redemption, "telegram_user_id": owner_id}
    notify_chat_id = (tg_user.id if tg_user else None) or owner_id
    logger.debug(
        "[purchase:redeem] redeem attempt code_last4=%s init_user_id=%s owner_id=%s notify_chat_id=%s",
        code_hint,
        tg_user.id if tg_user else None,
        owner_id,
        notify_chat_id,
    )
    ok, key, redeemed_entry = redeem_code(
        payload.code,
        redemption=redemption or None,
        form_expiry_option=form_expiry_opt,
    )
    if key == "expiry_mismatch":
        tier_entry = redeemed_entry or {}
        logger.info(
            "[purchase:redeem] failed reason=expiry_mismatch code_last4=%s code_requires=%s form_expiry=%s",
            code_hint,
            tier_entry.get("expiry_option"),
            form_expiry_opt,
        )
        log_event(
            "payment_code_redeem_failed",
            source="mini_app",
            telegram_user_id=ident["telegram_user_id"],
            username=ident["username"],
            first_name=ident["first_name"],
            meta={
                **_request_meta(request),
                "reason": "expiry_mismatch",
                "code_last4": code_hint,
                "code_requires": tier_entry.get("expiry_option"),
                "form_expiry": form_expiry_opt,
            },
        )
        raise HTTPException(status_code=400, detail="code_expiry_mismatch")
    if ok:
        already_pdf_sent = bool((redeemed_entry or {}).get("telegram_pdf_sent"))
        form_dict = payload.form.model_dump(exclude_none=True) if payload.form else None
        request_meta = _request_meta(request)
        entry_snapshot = redeemed_entry or {}
        notify_chat_id_enriched = notify_chat_id
        owner_id_enriched = owner_id
        redemption_enriched = redemption
        ident_enriched = ident
        # Client may redeem without initData/session; crypto codes still carry purchaser/order linkage.
        if notify_chat_id_enriched is None:
            ptid = entry_snapshot.get("purchaser_telegram_user_id")
            if ptid is not None:
                try:
                    tid_int = int(ptid)
                    notify_chat_id_enriched = tid_int
                    owner_id_enriched = owner_id_enriched or tid_int
                    redemption_enriched = {**(redemption_enriched or {}), "telegram_user_id": tid_int}
                    ident_enriched = {**ident_enriched, "telegram_user_id": tid_int}
                    logger.info(
                        "[purchase:redeem] notify target from stored purchaser telegram_user_id=%s",
                        tid_int,
                    )
                except (TypeError, ValueError):
                    logger.info(
                        "[purchase:redeem] purchaser_telegram_user_id present but invalid raw=%r code_last4=%s",
                        ptid,
                        code_hint,
                    )
            else:
                logger.info(
                    "[purchase:redeem] no purchaser_telegram_user_id on code entry source=%s order_id=%s code_last4=%s",
                    entry_snapshot.get("source"),
                    entry_snapshot.get("order_id"),
                    code_hint,
                )
            if notify_chat_id_enriched is None:
                oid = entry_snapshot.get("order_id")
                if oid and str(entry_snapshot.get("source", "")).lower() == "crypto":
                    ord_rec = get_order(str(oid))
                    tu = ord_rec.get("telegram_user_id") if ord_rec else None
                    if tu is not None:
                        try:
                            tid_int = int(tu)
                            notify_chat_id_enriched = tid_int
                            owner_id_enriched = owner_id_enriched or tid_int
                            redemption_enriched = {
                                **(redemption_enriched or {}),
                                "telegram_user_id": tid_int,
                            }
                            ident_enriched = {**ident_enriched, "telegram_user_id": tid_int}
                            logger.info(
                                "[purchase:redeem] notify target from crypto order order_id=%s telegram_user_id=%s",
                                oid,
                                tid_int,
                            )
                        except (TypeError, ValueError):
                            pass
                    elif ord_rec is not None:
                        logger.info(
                            "[purchase:redeem] crypto order %s has no telegram_user_id; cannot infer notify chat",
                            oid,
                        )
        logger.info(
            "[purchase:redeem] code accepted code_last4=%s owner_id=%s already_telegram_pdf_sent=%s",
            code_hint,
            owner_id_enriched,
            already_pdf_sent,
        )
        try:
            log_event(
                "payment_code_redeemed",
                source="mini_app",
                telegram_user_id=owner_id_enriched,
                username=ident_enriched.get("username"),
                first_name=ident_enriched.get("first_name"),
                meta={
                    **request_meta,
                    "code_last4": code_hint,
                    "price_ils": float(redemption_enriched.get("expiry_option") or 0)
                    if isinstance(redemption_enriched, dict)
                    and str(redemption_enriched.get("expiry_option") or "").isdigit()
                    else None,
                    "redemption": redemption_enriched or {},
                    "telegram_pdf_sent": already_pdf_sent,
                    "telegram_user_resolved": bool(tg_user),
                },
            )
        except Exception:
            logger.exception(
                "payment_code_redeemed log failed after redeem (code already consumed)"
            )
        _redeem_payment_code_deliver(
            chat_id=notify_chat_id_enriched,
            ident=ident_enriched,
            norm=norm,
            code_hint=code_hint,
            form_dict=form_dict,
            redemption=redemption_enriched,
            request_meta=request_meta,
            tg_user_resolved=bool(tg_user),
            already_pdf_sent=already_pdf_sent,
            telegram_user_session=payload.telegram_user_session,
        )
        return {"ok": True}
    if key == "already_used":
        logger.info(
            "[purchase:redeem] failed reason=already_used code_last4=%s telegram_user_id=%s",
            code_hint,
            ident.get("telegram_user_id"),
        )
        log_event(
            "payment_code_redeem_failed",
            source="mini_app",
            telegram_user_id=ident["telegram_user_id"],
            username=ident["username"],
            first_name=ident["first_name"],
            meta={**_request_meta(request), "reason": "already_used", "code_last4": code_hint},
        )
        raise HTTPException(status_code=400, detail="code_already_used")
    log_event(
        "payment_code_redeem_failed",
        source="mini_app",
        telegram_user_id=ident["telegram_user_id"],
        username=ident["username"],
        first_name=ident["first_name"],
        meta={**_request_meta(request), "reason": "invalid", "code_last4": code_hint},
    )
    logger.info(
        "[purchase:redeem] failed reason=invalid_code code_last4=%s telegram_user_id=%s",
        code_hint,
        ident.get("telegram_user_id"),
    )
    raise HTTPException(status_code=400, detail="invalid_code")


@app.post("/generate-pdf")
def generate_pdf(
    payload: GeneratePdfRequest,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    authorization: str | None = Header(default=None),
    tg_init_data: str | None = Query(default=None),
) -> Response:
    if maintenance_mode_enabled():
        raise HTTPException(status_code=503, detail="maintenance_mode")
    tg_user = _telegram_user_from_webapp_request(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data,
        body_init_data=payload.telegram_init_data,
        tg_user_sess=payload.telegram_user_session,
        authorization=authorization,
    )
    # Only rate-limit preview (watermark=True) requests; final downloads are gated by single-use code.
    if payload.watermark:
        check_rate_limit("preview_pdf", request, tg_user)
    ident = _telegram_log_identity(tg_user)
    try:
        pdf_bytes = replace_fields(
            hebrew_full_name=payload.hebrew_full_name.strip(),
            english_full_name=payload.english_full_name.strip(),
            id_number=payload.id_number.strip(),
            expiration_date=payload.expiration_date.strip(),
            watermark=payload.watermark,
        )
    except WatermarkMissingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not generate PDF: {exc}") from exc

    dl_token = register_pdf_bytes(pdf_bytes, user_meta=ident)
    headers = {
        "Content-Disposition": f'inline; filename="{OUTPUT_PDF_FILENAME}"',
        # Mini App / Telegram WebView often blocks blob: downloads — clients can open this HTTPS path instead.
        "X-Pdf-Download-Path": f"/pdf-download/{dl_token}",
    }
    log_event(
        "pdf_generated",
        source="mini_app" if tg_user else "api",
        telegram_user_id=ident["telegram_user_id"],
        username=ident["username"],
        first_name=ident["first_name"],
        meta={
            **_request_meta(request, watermark=payload.watermark),
            "payment_status": "paid_final" if not payload.watermark else "preview_unpaid",
            "form": {
                "hebrew_full_name": payload.hebrew_full_name,
                "english_full_name": payload.english_full_name,
                "id_number": payload.id_number,
                "expiration_date": payload.expiration_date,
            },
        },
    )
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@app.get("/pdf-download/{token}")
def download_pdf_by_token(token: str, request: Request) -> Response:
    """HTTPS download URL for the same bytes as ``POST /generate-pdf`` (Telegram-friendly)."""
    record = get_pdf_record(token)
    if record is None:
        logger.warning("pdf_download miss token_prefix=%s", token[:16])
        raise HTTPException(status_code=404, detail="download_expired_or_invalid")
    data = record["pdf_blob"]
    logger.info("pdf_download ok bytes=%s token_prefix=%s", len(data), token[:12])
    log_event(
        "pdf_downloaded",
        source="mini_app",
        telegram_user_id=record.get("telegram_user_id"),
        username=record.get("username"),
        first_name=record.get("first_name"),
        meta=_request_meta(request),
    )
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{OUTPUT_PDF_FILENAME}"',
        },
    )


def replace_fields(
    *,
    hebrew_full_name: str,
    english_full_name: str,
    id_number: str,
    expiration_date: str,
    watermark: bool = False,
) -> bytes:
    ensure_required_files()
    if watermark:
        ensure_watermark_file()

    doc = fitz.open(stream=TEMPLATE_PDF.read_bytes(), filetype="pdf")
    page = doc[0]

    id_rect, id_point, exp_rect, exp_point = detect_id_exp_metrics(page)
    qr_rect = detect_qr_rect(page) or QR_RECT_FALLBACK

    for rect in (HEBREW_NAME_RECT, ENGLISH_NAME_RECT, id_rect, exp_rect, qr_rect):
        page.add_redact_annot(expand_rect(rect, 2), fill=(1, 1, 1))
    page.apply_redactions()

    draw_hebrew_name(
        page=page,
        text=hebrew_full_name,
    )
    draw_english_name(
        page=page,
        text=english_full_name,
    )
    draw_data_line(
        page=page,
        point=id_point,
        text=id_number,
        fontname="ArimoDataId",
    )
    if _contains_hebrew(expiration_date):
        draw_data_line_rtl(page=page, rect=exp_rect, text=expiration_date)
    else:
        draw_data_line(
            page=page,
            point=exp_point,
            text=expiration_date,
            fontname="ArimoDataExp",
        )
    draw_static_qr(page=page, rect=qr_rect)

    # Raster overlay last so template colors + filled fields stay underneath (semi-transparent PNG).
    if watermark:
        draw_watermark(page)

    # Second page: blank, same dimensions as the template page.
    pr = page.rect
    doc.new_page(pno=-1, width=pr.width, height=pr.height)

    sanitize_export_pdf(doc)
    output = BytesIO()
    doc.save(
        output,
        garbage=4,
        deflate=True,
        clean=True,
        preserve_metadata=0,
    )
    doc.close()
    return output.getvalue()


def ensure_required_files() -> None:
    missing = [
        path
        for path in (TEMPLATE_PDF, ARIMO_FONT, HEBREW_IMAGE_FONT)
        if not path.exists()
    ]
    if missing:
        missing_list = ", ".join(str(path.relative_to(ROOT_DIR)) for path in missing)
        raise FileNotFoundError(f"Missing required file(s): {missing_list}")


def ensure_watermark_file() -> None:
    if watermark_png_path() is None:
        raise WatermarkMissingError()


def watermark_png_path() -> Path | None:
    if WATERMARK_PNG_ASSETS.exists():
        return WATERMARK_PNG_ASSETS
    if WATERMARK_PNG_ROOT.exists():
        return WATERMARK_PNG_ROOT
    return None


def sanitize_export_pdf(doc: fitz.Document) -> None:
    """Clear /Info, XMP, and dates so exports avoid embedded creator/tooling trails."""
    doc.set_metadata(
        {
            "producer": "",
            "creator": "",
            "creationDate": "",
            "modDate": "",
            "title": "",
            "author": "",
            "subject": "",
            "keywords": "",
            "trapped": "",
        }
    )
    try:
        doc.del_xml_metadata()
    except Exception:
        pass


def raster_png_bytes_clean(path: Path) -> bytes:
    """Re-encode raster to plain RGBA PNG without EXIF/ICC/text ancillary chunks from source."""
    with Image.open(path) as im:
        im.load()
        rgba = im.convert("RGBA")
        rgba.info.clear()
        buf = BytesIO()
        rgba.save(buf, format="PNG", compress_level=9, optimize=True)
        return buf.getvalue()


def draw_watermark(page: fitz.Page) -> None:
    """Full-page PNG overlay; must run after text/QR so it appears on top of the filled card."""
    path = watermark_png_path()
    if path is None:
        raise FileNotFoundError("Watermark file missing")
    page.insert_image(
        page.rect,
        stream=raster_png_bytes_clean(path),
        overlay=True,
        keep_proportion=True,
    )


def expand_rect(rect: fitz.Rect, amount: float) -> fitz.Rect:
    return fitz.Rect(
        rect.x0 - amount,
        rect.y0 - amount,
        rect.x1 + amount,
        rect.y1 + amount,
    )


def detect_qr_rect(page: fitz.Page) -> fitz.Rect | None:
    """Pick the smallest square embedded image (template QR) by bounding box."""
    candidates: list[tuple[float, fitz.Rect]] = []
    for img in page.get_images(full=True):
        xref = img[0]
        for r in page.get_image_rects(xref):
            w, h = r.width, r.height
            if w < 12 or h < 12:
                continue
            area = w * h
            if area < 800:
                continue
            rel = abs(w - h) / max(w, h)
            if rel <= 0.03:
                candidates.append((area, r))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def detect_id_exp_metrics(page: fitz.Page) -> tuple[fitz.Rect, fitz.Point, fitz.Rect, fitz.Point]:
    """Locate ID and expiration spans from the template so edits track layout changes."""
    id_rect: fitz.Rect | None = None
    id_point: fitz.Point | None = None
    exp_rect: fitz.Rect | None = None
    exp_point: fitz.Point | None = None

    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                bbox = span.get("bbox")
                origin = span.get("origin")
                if not text or not bbox or not origin:
                    continue
                if bbox[1] < 580:
                    continue
                if re.fullmatch(r"\d{6,12}", text):
                    id_rect = fitz.Rect(bbox)
                    id_point = fitz.Point(origin[0], origin[1])
                elif re.fullmatch(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}", text):
                    exp_rect = fitz.Rect(bbox)
                    exp_point = fitz.Point(origin[0], origin[1])

    return (
        id_rect or ID_NUMBER_RECT,
        id_point or ID_NUMBER_BASELINE,
        exp_rect or EXPIRATION_RECT,
        exp_point or EXPIRATION_BASELINE,
    )


def draw_hebrew_name(*, page: fitz.Page, text: str) -> None:
    image_bytes, width_pt, height_pt = render_hebrew_name_image(text)
    rect = fitz.Rect(
        HEBREW_RIGHT_EDGE - width_pt,
        HEBREW_TOP,
        HEBREW_RIGHT_EDGE,
        HEBREW_TOP + height_pt,
    )
    page.insert_image(rect, stream=image_bytes, overlay=True)


def draw_english_name(*, page: fitz.Page, text: str) -> None:
    display = text.strip().upper()
    font = fitz.Font(fontfile=str(ARIMO_FONT))
    text_width = font.text_length(display, fontsize=NAME_FONT_SIZE)
    # Right-align to the same right edge as the Hebrew name.
    x = HEBREW_RIGHT_EDGE - text_width
    page.insert_text(
        fitz.Point(x, ENGLISH_BASELINE.y),
        display,
        fontsize=NAME_FONT_SIZE,
        fontfile=str(ARIMO_FONT),
        fontname="ArimoNameEn",
        color=(0, 0, 0),
    )


def render_qr_png(data: str, box_px: int) -> bytes:
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    img = img.resize((box_px, box_px), Image.Resampling.NEAREST)
    img.info.clear()
    buf = BytesIO()
    img.save(buf, format="PNG", compress_level=9, optimize=True)
    return buf.getvalue()


def load_static_qr_payload() -> str:
    if STATIC_QR_PAYLOAD_TXT.exists():
        text = STATIC_QR_PAYLOAD_TXT.read_text(encoding="utf-8").strip()
        if text:
            return text
    return DEFAULT_STATIC_QR_PAYLOAD


def draw_static_qr(*, page: fitz.Page, rect: fitz.Rect) -> None:
    """Always the same QR: image file wins; otherwise encode fixed payload text."""
    if STATIC_QR_PNG.exists():
        page.insert_image(
            rect,
            stream=raster_png_bytes_clean(STATIC_QR_PNG),
            overlay=True,
            keep_proportion=True,
        )
    else:
        draw_qr(page=page, rect=rect, content=load_static_qr_payload())


def draw_qr(*, page: fitz.Page, rect: fitz.Rect, content: str) -> None:
    edge_pt = max(rect.width, rect.height)
    px = max(256, int(edge_pt * 4))
    png = render_qr_png(content.strip(), box_px=px)
    page.insert_image(rect, stream=png, overlay=True, keep_proportion=True)


def draw_data_line(
    *,
    page: fitz.Page,
    point: fitz.Point,
    text: str,
    fontname: str = "ArimoData",
) -> None:
    page.insert_text(
        point,
        text,
        fontsize=DATA_FONT_SIZE,
        fontfile=str(ARIMO_FONT),
        fontname=fontname,
        color=(0, 0, 0),
    )


def _render_rtl_image(text: str, font_size_pt: float) -> tuple[bytes, float, float]:
    """Render any RTL/Hebrew text as a transparent PIL PNG. Returns (png_bytes, width_pt, height_pt)."""
    scale = 4
    font_size_px = round(font_size_pt * scale)
    font = ImageFont.truetype(str(HEBREW_IMAGE_FONT), font_size_px)
    visual_text = text[::-1]

    scratch = Image.new("RGBA", (1, 1), (255, 255, 255, 0))
    draw = ImageDraw.Draw(scratch)
    bbox = draw.textbbox((0, 0), visual_text, font=font)
    width = max(1, bbox[2] - bbox[0])
    height = max(1, bbox[3] - bbox[1])
    image = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.text((-bbox[0], -bbox[1]), visual_text, font=font, fill=(0, 0, 0, 255))

    output = BytesIO()
    image.info.clear()
    image.save(output, format="PNG", compress_level=9, optimize=True)
    return output.getvalue(), image.width / scale, image.height / scale


def render_hebrew_name_image(text: str) -> tuple[bytes, float, float]:
    return _render_rtl_image(text, NAME_FONT_SIZE)


def _contains_hebrew(text: str) -> bool:
    return any("\u05d0" <= ch <= "\u05ea" for ch in text)


def draw_data_line_rtl(*, page: fitz.Page, rect: fitz.Rect, text: str) -> None:
    """Render an RTL (Hebrew) data value as a PIL image, right-aligned within rect."""
    image_bytes, width_pt, height_pt = _render_rtl_image(text, DATA_FONT_SIZE)
    x_right = rect.x1
    x_left = max(rect.x0, x_right - width_pt)
    y_top = rect.y0 + max(0.0, (rect.height - height_pt) / 2)
    img_rect = fitz.Rect(x_left, y_top, x_right, y_top + height_pt)
    page.insert_image(img_rect, stream=image_bytes, overlay=True)

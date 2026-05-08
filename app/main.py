from __future__ import annotations

from io import BytesIO
import logging
import os
import re
import secrets
import uuid
from pathlib import Path
from typing import Any

import fitz
import qrcode
from qrcode.constants import ERROR_CORRECT_M
import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

from app.activity_store import list_events, list_user_directory, log_event, summary as activity_summary
from app.admin_auth import (
    AdminIdentity,
    effective_admin_secret,
    require_admin,
    resolve_telegram_webapp_user,
)
from app.admin_control import get_control_state, maintenance_mode_enabled, set_maintenance_mode
from app.crypto_orders import create_order, get_order, list_orders, mark_paid, mark_pdf_sent
from app.nowpayments import create_invoice as nowpayments_create_invoice, verify_ipn_signature
from app.payment_codes_store import (
    get_code_entry,
    issue_new_code,
    mark_code_telegram_pdf_sent,
    normalize_code,
    redeem_code,
)
from app.pdf_download_cache import get_pdf_record, register_pdf_bytes
from app.public_url import effective_public_base_url
from app.rate_limits import check_rate_limit, delete_override, list_overrides, upsert_override
from app.telegram_notify import (
    get_telegram_chat_profile,
    send_telegram_document,
    send_telegram_document_url,
    send_telegram_message,
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
    telegram_user_id: int | None = None
    username: str | None = Field(default=None, max_length=64)
    first_name: str | None = Field(default=None, max_length=64)
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
    telegram_init_data: str | None = Field(default=None, max_length=16000)
    telegram_user_session: str | None = Field(default=None, max_length=1000)


class RateLimitOverrideRequest(BaseModel):
    telegram_user_id: int
    expires_at: str | None = Field(default=None, max_length=48)
    bypass: bool = True
    multiplier: float = Field(default=2.0, ge=1.0, le=100.0)
    notes: str | None = Field(default=None, max_length=240)


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


def _payment_code_matches_form_expiry(code_entry: dict[str, Any], form: RedeemFormSnapshot | None) -> bool:
    """Tier-specific codes require the Mini App expiry package to match issuance."""
    if code_entry.get("issue_scope") != "tier":
        return True
    required = code_entry.get("expiry_option")
    if not required:
        return True
    if form is None or not form.expiry_option:
        return False
    return str(form.expiry_option).strip() == str(required).strip()


def _index_html_response() -> HTMLResponse:
    """Serve built React SPA when ``dist/index.html`` exists (Docker/Render); else dev ``index.html``."""
    dist_index = DIST_DIR / "index.html"
    if dist_index.is_file():
        raw = dist_index.read_text(encoding="utf-8")
        return HTMLResponse(content=raw, media_type="text/html; charset=utf-8")
    path = ROOT_DIR / "index.html"
    raw = path.read_text(encoding="utf-8")
    return HTMLResponse(content=raw, media_type="text/html; charset=utf-8")


app = FastAPI(title="PDF Name Editor")


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
    expose_headers=["X-Pdf-Download-Path"],
)


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
    return resolve_telegram_webapp_user(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data_query,
        body_init_data=body_init_data,
        tg_user_sess=tg_user_sess,
        authorization=auth,
    )


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
        raise HTTPException(status_code=401, detail="telegram_user_required")
    return user


def _telegram_log_identity(tg_user) -> dict[str, Any]:
    if tg_user is None:
        return {"telegram_user_id": None, "username": None, "first_name": None}
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
        meta={"saved_form_id": row["id"], "title": row.get("title")},
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


@app.get("/api/admin/summary")
def admin_summary(_: AdminIdentity = Depends(require_admin)) -> dict:
    from app.payment_codes_store import codes_summary

    return {
        "activity": activity_summary(),
        "payment_codes": codes_summary(),
        "control": get_control_state(),
    }


@app.get("/api/admin/users")
def admin_users(
    _: AdminIdentity = Depends(require_admin),
    limit: int = Query(default=150, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return list_user_directory(limit=limit, offset=offset)


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
    if not chat_id:
        log_event(
            "telegram_delivery_failed",
            source=event_source,
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            meta={**meta, "error": "no_chat_id"},
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
        return True

    logger.warning(
        "Telegram sendDocument multipart failed (will try URL): user=%s err=%s",
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
                    "Telegram sendDocument URL failed: url=%s err=%s",
                    pdf_url,
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
                return True
        except Exception as exc:
            err = f"url_method_exception: {exc}"
            logger.exception("Telegram URL delivery failed")
    else:
        err = "public_base_url_not_configured"
        logger.warning(
            "No public base URL for Telegram URL fallback (set WEB_APP_URL or Railway/Render domain)"
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
    logger.warning("telegram_pdf_delivery_failed user=%s err=%s", telegram_user_id, err)
    return False


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
) -> None:
    """Notify Telegram + log redemption after the HTTP response returns (avoids blocking the client)."""
    uid = ident.get("telegram_user_id")
    un = ident.get("username")
    fn = ident.get("first_name")
    telegram_pdf_sent = already_pdf_sent
    if chat_id and not already_pdf_sent:
        try:
            pdf_bytes: bytes | None = None
            if form_dict:
                pdf_bytes = _pdf_from_form_snapshot(form_dict)
            if pdf_bytes is None and redemption:
                pdf_bytes = _pdf_from_form_snapshot(redemption)
            if pdf_bytes is None:
                log_event(
                    "telegram_pdf_skipped_no_form_data",
                    source="mini_app",
                    telegram_user_id=uid,
                    username=un,
                    first_name=fn,
                    meta={
                        "code_last4": code_hint,
                        "form_received": bool(form_dict),
                        "form_fields": form_dict or {},
                    },
                )
            if _send_final_pdf_to_telegram(
                chat_id=chat_id,
                pdf_bytes=pdf_bytes,
                event_source="mini_app",
                telegram_user_id=uid,
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
                telegram_user_id=uid,
                username=un,
                first_name=fn,
                meta={
                    "code_last4": code_hint,
                    "error": str(exc),
                    "exception_type": type(exc).__name__,
                },
            )
    if chat_id:
        ok_msg, err_msg = send_telegram_message(
            chat_id,
            "✅ <b>תשלום אושר במערכת</b>\n\n"
            + (
                "📎 קובץ PDF סופי נשלח גם כאן בצ׳אט."
                if telegram_pdf_sent
                else "ניתן להוריד את הקובץ הסופי מהמיני־אפליקציה."
            ),
        )
        if not ok_msg:
            log_event(
                "telegram_delivery_failed",
                source="mini_app",
                telegram_user_id=uid,
                username=un,
                first_name=fn,
                meta={
                    "code_last4": code_hint,
                    "kind": "redeem_confirmation",
                    "error": err_msg,
                },
            )
    else:
        log_event(
            "telegram_notify_skipped_no_user",
            source="mini_app",
            meta={
                "code_last4": code_hint,
                "reason": "missing_or_invalid_init_data",
            },
        )
    log_event(
        "payment_code_redeemed",
        source="mini_app",
        telegram_user_id=uid,
        username=un,
        first_name=fn,
        meta={
            **request_meta,
            "code_last4": code_hint,
            "redemption": redemption,
            "telegram_pdf_sent": telegram_pdf_sent,
            "telegram_user_resolved": tg_user_resolved,
        },
    )


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
        raise HTTPException(status_code=503, detail="WEB_APP_URL not configured")

    order_id = str(uuid.uuid4())
    tg_user = _telegram_user_from_webapp_request(
        request,
        x_telegram_init_data=x_telegram_init_data,
        tg_init_data_query=tg_init_data,
        body_init_data=payload.telegram_init_data,
        tg_user_sess=payload.telegram_user_session,
        authorization=authorization,
    )
    check_rate_limit("create_invoice", request, tg_user)
    ident = _telegram_log_identity(tg_user)

    # Prefer init-data user over payload (init-data is server-verified)
    real_user_id = ident["telegram_user_id"] or payload.telegram_user_id
    real_username = ident["username"] or payload.username
    real_first_name = ident["first_name"] or payload.first_name
    form_snapshot = _build_redemption_dict(tg_user, payload.form)

    description = f"פטור מתור · {payload.expiry_option or ''} · ₪{int(payload.price_ils)}"

    try:
        invoice = await nowpayments_create_invoice(
            price_amount=payload.price_ils,
            price_currency="ils",
            order_id=order_id,
            order_description=description,
            ipn_callback_url=f"{base_url}/api/crypto/ipn",
            success_url=f"{base_url}/static/index.html?crypto_order={order_id}",
            cancel_url=f"{base_url}/static/index.html",
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502, detail=f"NOWPayments error: {exc.response.text[:200]}"
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
        price_ils=payload.price_ils,
        expiry_option=payload.expiry_option,
        invoice_url=invoice_url,
        form=form_snapshot or None,
    )
    log_event(
        "crypto_invoice_created",
        source="mini_app",
        telegram_user_id=real_user_id,
        username=real_username,
        first_name=real_first_name,
        meta={
            "order_id": order_id,
            "price_ils": payload.price_ils,
            "expiry_option": payload.expiry_option,
            "form": form_snapshot,
        },
    )
    return {"order_id": order_id, "invoice_url": invoice_url}


@app.get("/api/crypto/order-status")
def crypto_order_status(order_id: str = Query(..., min_length=4)) -> dict:
    """Poll order payment status (used by Mini App after opening invoice URL)."""
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
    raw_body = await request.body()
    sig = request.headers.get("x-nowpayments-sig", "")

    if not verify_ipn_signature(raw_body, sig):
        raise HTTPException(status_code=400, detail="invalid_ipn_signature")

    try:
        data: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_json")

    payment_status = (data.get("payment_status") or "").lower()
    order_id = data.get("order_id", "")

    # Only act on confirmed/finished payments
    if payment_status not in {"finished", "confirmed", "partially_paid"}:
        return JSONResponse({"ok": True, "ignored": True, "payment_status": payment_status})

    if not order_id:
        return JSONResponse({"ok": True, "ignored": True, "reason": "no_order_id"})

    order = get_order(order_id)
    if order is None:
        return JSONResponse({"ok": True, "ignored": True, "reason": "order_not_found"})
    if order.get("status") == "paid":
        return JSONResponse({"ok": True, "updated": False, "reason": "already_paid"})

    # Issue a one-time payment code for this order
    code = issue_new_code(meta={"source": "crypto", "order_id": order_id})
    updated = mark_paid(order_id=order_id, payment_code=code, ipn_payload=data)

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
            "already_processed": not updated,
        },
    )

    # Notify the user via Telegram if we know their chat id
    tg_user_id = order.get("telegram_user_id")
    if tg_user_id and updated:
        pdf_sent = False
        try:
            pdf_bytes = _pdf_from_form_snapshot(order.get("form"))
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
        ok_msg, err_msg = send_telegram_message(
            tg_user_id,
            "✅ <b>התשלום התקבל ואושר</b>\n\n"
            f"🔑 קוד האישור שלכם: <code>{code}</code>\n\n"
            + (
                "📎 קובץ PDF סופי נשלח גם כאן בצ׳אט."
                if pdf_sent
                else "יש לחזור למיני־אפליקציה, להזין את הקוד בשדה אישור התשלום ולהוריד את הקובץ."
            ),
        )
        if not ok_msg:
            log_event(
                "telegram_delivery_failed",
                source="nowpayments_ipn",
                telegram_user_id=tg_user_id,
                username=order.get("username"),
                first_name=order.get("first_name"),
                meta={"order_id": order_id, "kind": "crypto_payment_notice", "error": err_msg},
            )

    return JSONResponse({"ok": True, "updated": updated})


@app.get("/api/admin/crypto-orders")
def admin_crypto_orders(
    _: AdminIdentity = Depends(require_admin),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return list_orders(limit=limit, offset=offset)


@app.post("/redeem-payment-code")
def redeem_payment_code(
    payload: RedeemPaymentCodeRequest,
    request: Request,
    background_tasks: BackgroundTasks,
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

    pending_entry = get_code_entry(norm) if len(norm) >= 8 else None
    if pending_entry is not None and not pending_entry.get("used"):
        if not _payment_code_matches_form_expiry(pending_entry, payload.form):
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
                    "code_requires": pending_entry.get("expiry_option"),
                    "form_expiry": payload.form.expiry_option if payload.form else None,
                },
            )
            raise HTTPException(status_code=400, detail="code_expiry_mismatch")

    redemption = _build_redemption_dict(tg_user, payload.form)
    ok, key, redeemed_entry = redeem_code(payload.code, redemption=redemption or None)
    if ok:
        already_pdf_sent = bool((pending_entry or redeemed_entry or {}).get("telegram_pdf_sent"))
        form_dict = payload.form.model_dump(exclude_none=True) if payload.form else None
        request_meta = _request_meta(request)
        background_tasks.add_task(
            _redeem_payment_code_deliver,
            chat_id=tg_user.id if tg_user else None,
            ident=ident,
            norm=norm,
            code_hint=code_hint,
            form_dict=form_dict,
            redemption=redemption,
            request_meta=request_meta,
            tg_user_resolved=bool(tg_user),
            already_pdf_sent=already_pdf_sent,
        )
        return {"ok": True}
    if key == "already_used":
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

    # Watermark must be drawn before editable text so all fields stay visible on top.
    if watermark:
        draw_watermark(page)

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
    """Place raster watermark on top of existing page content (last draw wins)."""
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

from __future__ import annotations

from io import BytesIO
import os
import re
from pathlib import Path
from typing import Any

import fitz
import qrcode
from qrcode.constants import ERROR_CORRECT_M
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

from app.activity_store import list_events, list_user_directory, log_event, summary as activity_summary
from app.admin_auth import AdminIdentity, effective_admin_secret, parse_optional_telegram_user, require_admin
from app.admin_control import get_control_state, maintenance_mode_enabled, set_maintenance_mode
from app.payment_codes_store import normalize_code, redeem_code
from app.pdf_download_cache import get_pdf_bytes, register_pdf_bytes


ROOT_DIR = Path(__file__).resolve().parents[1]
ASSETS_DIR = ROOT_DIR / "assets"
FONTS_DIR = ROOT_DIR / "fonts"
STATIC_DIR = ROOT_DIR / "static"
DIST_DIR = ROOT_DIR / "dist"
DIST_ASSETS_DIR = DIST_DIR / "assets"

OUTPUT_PDF_FILENAME = "FormPDFPreview.pdf"


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


class RedeemFormSnapshot(BaseModel):
    hebrew_full_name: str | None = Field(default=None, max_length=120)
    english_full_name: str | None = Field(default=None, max_length=120)
    id_number: str | None = Field(default=None, max_length=32)
    expiration_date: str | None = Field(default=None, max_length=48)
    expiry_option: str | None = Field(default=None, max_length=32)


class RedeemPaymentCodeRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=64)
    form: RedeemFormSnapshot | None = None


class MaintenanceModeRequest(BaseModel):
    enabled: bool


def _build_redemption_dict(tg_user: Any, form: RedeemFormSnapshot | None) -> dict[str, Any]:
    """Persist Mini App user + optional form snapshot when a payment code is redeemed."""
    out: dict[str, Any] = {}
    if tg_user:
        out["telegram_user_id"] = tg_user.id
        if tg_user.username:
            out["username"] = tg_user.username
        if tg_user.first_name:
            out["first_name"] = tg_user.first_name
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


def _telegram_user_from_header(init_data: str | None):
    return parse_optional_telegram_user(init_data)


def _request_meta(request: Request, *, watermark: bool | None = None) -> dict[str, str | bool | None]:
    ua = request.headers.get("user-agent", "")
    return {
        "watermark": watermark,
        "client": "telegram" if request.headers.get("x-telegram-init-data") else "web",
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
    return list_events(
        limit=limit,
        offset=offset,
        event_type=event_type,
        telegram_user_id=telegram_user_id,
    )


@app.get("/api/admin/payment-codes")
def admin_payment_codes(_: AdminIdentity = Depends(require_admin)) -> dict:
    from app.payment_codes_store import list_codes

    return {"items": list_codes(include_code=True)}


@app.post("/api/admin/codes/issue")
def admin_issue_payment_code(_: AdminIdentity = Depends(require_admin)) -> dict[str, str]:
    from app.payment_codes_store import issue_new_code

    code = issue_new_code()
    log_event("payment_code_issued", source="admin")
    return {"code": code}


@app.get("/api/admin/control")
def admin_control(_: AdminIdentity = Depends(require_admin)) -> dict:
    return get_control_state()


@app.post("/api/admin/maintenance")
def admin_maintenance(payload: MaintenanceModeRequest, _: AdminIdentity = Depends(require_admin)) -> dict:
    state = set_maintenance_mode(payload.enabled)
    log_event("maintenance_changed", source="admin", meta={"enabled": payload.enabled})
    return state


@app.post("/redeem-payment-code")
def redeem_payment_code(
    payload: RedeemPaymentCodeRequest,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
) -> dict[str, bool | str]:
    """Validate and consume a one-time code issued via the Telegram bot."""
    tg_user = _telegram_user_from_header(x_telegram_init_data)
    redemption = _build_redemption_dict(tg_user, payload.form)
    ok, key = redeem_code(payload.code, redemption=redemption or None)
    norm = normalize_code(payload.code)
    code_hint = norm[-4:].upper() if len(norm) >= 4 else None
    if ok:
        log_event(
            "payment_code_redeemed",
            source="mini_app",
            telegram_user_id=tg_user.id if tg_user else None,
            username=tg_user.username if tg_user else None,
            first_name=tg_user.first_name if tg_user else None,
            meta={**_request_meta(request), "code_last4": code_hint, "redemption": redemption},
        )
        return {"ok": True}
    if key == "already_used":
        log_event(
            "payment_code_redeem_failed",
            source="mini_app",
            telegram_user_id=tg_user.id if tg_user else None,
            username=tg_user.username if tg_user else None,
            first_name=tg_user.first_name if tg_user else None,
            meta={**_request_meta(request), "reason": "already_used", "code_last4": code_hint},
        )
        raise HTTPException(status_code=400, detail="code_already_used")
    log_event(
        "payment_code_redeem_failed",
        source="mini_app",
        telegram_user_id=tg_user.id if tg_user else None,
        username=tg_user.username if tg_user else None,
        first_name=tg_user.first_name if tg_user else None,
        meta={**_request_meta(request), "reason": "invalid", "code_last4": code_hint},
    )
    raise HTTPException(status_code=400, detail="invalid_code")


@app.post("/generate-pdf")
def generate_pdf(
    payload: GeneratePdfRequest,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
) -> Response:
    if maintenance_mode_enabled():
        raise HTTPException(status_code=503, detail="maintenance_mode")
    tg_user = _telegram_user_from_header(x_telegram_init_data)
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

    dl_token = register_pdf_bytes(pdf_bytes)
    headers = {
        "Content-Disposition": f'inline; filename="{OUTPUT_PDF_FILENAME}"',
        # Mini App / Telegram WebView often blocks blob: downloads — clients can open this HTTPS path instead.
        "X-Pdf-Download-Path": f"/pdf-download/{dl_token}",
    }
    log_event(
        "pdf_generated",
        source="mini_app" if tg_user else "api",
        telegram_user_id=tg_user.id if tg_user else None,
        username=tg_user.username if tg_user else None,
        first_name=tg_user.first_name if tg_user else None,
        meta={
            **_request_meta(request, watermark=payload.watermark),
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
    data = get_pdf_bytes(token)
    if data is None:
        raise HTTPException(status_code=404, detail="download_expired_or_invalid")
    log_event("pdf_downloaded", source="mini_app", meta=_request_meta(request))
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

"""Smart Telegram owner notifications for business-critical events."""

from __future__ import annotations

import html
import logging
import os
from typing import Any

from app.telegram_notify import send_telegram_message

logger = logging.getLogger(__name__)

_DEFAULT_ADMIN_NOTIFY_CHAT_ID = "-1003569464018"


def admin_notify_chat_id() -> int | None:
    raw = (
        os.environ.get("TELEGRAM_ADMIN_NOTIFY_CHAT_ID")
        or os.environ.get("TELEGRAM_START_NOTIFY_CHAT_ID")
        or _DEFAULT_ADMIN_NOTIFY_CHAT_ID
    ).strip()
    if raw.lower() in ("", "0", "false", "off", "none", "-", "disable"):
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid TELEGRAM_ADMIN_NOTIFY_CHAT_ID: %r", raw)
        return None


def _user_line(telegram_user_id: int | None, username: str | None, first_name: str | None) -> str:
    parts: list[str] = []
    if first_name:
        parts.append(html.escape(first_name))
    if username:
        parts.append("@" + html.escape(username))
    if telegram_user_id is not None:
        parts.append(f"<code>{telegram_user_id}</code>")
    return " | ".join(parts) if parts else "-"


def _money(value: Any) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return f"{n:.0f} ILS"


def _event_title(event_type: str, meta: dict[str, Any]) -> str | None:
    if event_type == "mini_app_opened":
        return "Mini App opened"
    if event_type == "mini_app_form_started":
        return "User started filling"
    if event_type == "mini_app_payment_screen":
        return "User reached payment"
    if event_type == "mini_app_abandoned":
        return "User left before payment"
    if event_type == "manual_payment_requested":
        return "Manual payment requested"
    if event_type == "crypto_invoice_created":
        return "Crypto invoice created"
    if event_type == "crypto_payment_confirmed":
        return "Payment succeeded"
    if event_type == "payment_code_redeemed":
        return "Payment code redeemed"
    if event_type == "payment_code_redeem_failed":
        return "Payment failed"
    if event_type == "telegram_final_pdf_sent":
        return "PDF sent"
    if event_type == "pdf_generated" and meta.get("payment_status") == "paid_final":
        return "Final PDF generated"
    return None


def send_admin_event_notification(
    event_type: str,
    *,
    source: str,
    telegram_user_id: int | None = None,
    username: str | None = None,
    first_name: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    payload = meta or {}
    if payload.get("autosave") and event_type == "saved_form_upserted":
        return
    title = _event_title(event_type, payload)
    chat_id = admin_notify_chat_id()
    if not title or chat_id is None:
        return

    lines = [
        f"<b>{html.escape(title)}</b>",
        f"<b>Event:</b> <code>{html.escape(event_type)}</code>",
        f"<b>Source:</b> {html.escape(source)}",
        f"<b>User:</b> {_user_line(telegram_user_id, username, first_name)}",
    ]
    for key in ("price_ils", "final_price_ils"):
        money = _money(payload.get(key))
        if money:
            lines.append(f"<b>Amount:</b> {html.escape(money)}")
            break
    if payload.get("discount_ils"):
        lines.append(f"<b>Discount:</b> {html.escape(_money(payload.get('discount_ils')))}")
    if payload.get("coupon_code"):
        lines.append(f"<b>Coupon:</b> <code>{html.escape(str(payload.get('coupon_code')))}</code>")
    if payload.get("method"):
        lines.append(f"<b>Method:</b> {html.escape(str(payload.get('method')))}")
    if payload.get("expiry_option"):
        lines.append(f"<b>Package:</b> {html.escape(str(payload.get('expiry_option')))}")
    if payload.get("order_id"):
        lines.append(f"<b>Order:</b> <code>{html.escape(str(payload.get('order_id')))}</code>")
    if payload.get("code_last4"):
        lines.append(f"<b>Code:</b> ****{html.escape(str(payload.get('code_last4')))}")
    reason = payload.get("reason")
    if reason:
        lines.append(f"<b>Reason:</b> {html.escape(str(reason))}")
    form = payload.get("form")
    if isinstance(form, dict):
        form_lines: list[str] = []
        for key, label in (
            ("hebrew_full_name", "Hebrew name"),
            ("english_full_name", "English name"),
            ("id_number", "ID"),
            ("expiration_date", "Expiration"),
        ):
            value = form.get(key)
            if value:
                form_lines.append(f"{label}: {html.escape(str(value))}")
        if form_lines:
            lines.append("<b>Order details:</b>\n" + "\n".join(form_lines))

    ok, err = send_telegram_message(chat_id, "\n".join(lines))
    if not ok:
        logger.warning("admin event notification failed event_type=%s err=%s", event_type, err)

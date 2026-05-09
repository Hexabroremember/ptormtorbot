"""NOWPayments API client + IPN signature verification."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

NOWPAYMENTS_BASE = "https://api.nowpayments.io/v1"

_API_KEY_ALIASES = frozenset(
    {
        "NOWPAYMENTS_API_KEY",
        "NOW_PAYMENTS_API_KEY",
        "NOWPAYMENTS_KEY",
    }
)
_IPN_ALIASES = frozenset(
    {
        "NOWPAYMENTS_IPN_SECRET",
        "NOW_PAYMENTS_IPN_SECRET",
    }
)


def _normalize_env_name(name: str) -> str:
    return name.strip().upper().replace("-", "_")


def _env_value_for_aliases(aliases: frozenset[str]) -> str:
    """Resolve first non-empty value; exact name first, then case-insensitive key match."""
    for name in aliases:
        v = os.environ.get(name, "").strip()
        if v:
            return v
    wanted = {_normalize_env_name(a) for a in aliases}
    for k, v in os.environ.items():
        if _normalize_env_name(k) in wanted and v.strip():
            return v.strip()
    return ""


def _api_key() -> str:
    return _env_value_for_aliases(_API_KEY_ALIASES)


def nowpayments_key_configured() -> bool:
    return bool(_api_key())


def related_payment_env_names() -> list[str]:
    """Names only — for logs when debugging Railway variable typos."""
    out: list[str] = []
    for k in os.environ:
        lk = k.lower()
        if "nowpay" in lk or "now_payment" in lk or ("payment" in lk and "now" in lk):
            out.append(k)
    return sorted(out)


def _ipn_secret() -> str:
    return _env_value_for_aliases(_IPN_ALIASES)


async def create_invoice(
    *,
    price_amount: float,
    price_currency: str = "ils",
    order_id: str,
    order_description: str,
    ipn_callback_url: str,
    success_url: str,
    cancel_url: str,
) -> dict[str, Any]:
    """Create a NOWPayments invoice; returns the full API response (includes invoice_url)."""
    key = _api_key()
    if not key:
        raise ValueError(
            "NOWPayments API key missing: set NOWPAYMENTS_API_KEY on the **same** "
            "service that runs the API (e.g. Railway → your web service → Variables), "
            "then redeploy. Frontend-only env vars are not visible to the backend."
        )

    payload = {
        "price_amount": price_amount,
        "price_currency": price_currency,
        "order_id": order_id,
        "order_description": order_description,
        "ipn_callback_url": ipn_callback_url,
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    logger.debug(
        "[purchase:nowpayments] invoice request order_id=%s price=%s %s ipn_host=%s",
        order_id,
        price_amount,
        price_currency,
        ipn_callback_url.split("://", 1)[-1].split("/", 1)[0] if "://" in ipn_callback_url else "(relative)",
    )
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{NOWPAYMENTS_BASE}/invoice",
            json=payload,
            headers={"x-api-key": key, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    logger.info(
        "[purchase:nowpayments] invoice created order_id=%s has_invoice_url=%s",
        order_id,
        bool(data.get("invoice_url") or data.get("invoiceUrl")),
    )
    return data


def verify_ipn_signature(raw_body: bytes, signature: str) -> bool:
    """
    Validate NOWPayments IPN request.
    Algorithm: sort JSON keys → HMAC-SHA512 with IPN secret.
    Returns True when no secret is configured (allows testing without it).
    """
    secret = _ipn_secret()
    if not secret:
        logger.debug(
            "[purchase:nowpayments_ipn] signature check skipped (NOWPAYMENTS_IPN_SECRET unset) body_len=%s",
            len(raw_body or b""),
        )
        return True
    if not signature:
        logger.warning("[purchase:nowpayments_ipn] signature missing but secret is configured")
        return False
    try:
        data: dict[str, Any] = json.loads(raw_body)
        sorted_payload = json.dumps(
            dict(sorted(data.items())), separators=(",", ":"), ensure_ascii=False
        )
        expected = hmac.new(
            secret.encode("utf-8"),
            sorted_payload.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()
        ok = hmac.compare_digest(expected.lower(), signature.lower())
        if not ok:
            logger.warning(
                "[purchase:nowpayments_ipn] signature mismatch order_id=%s payment_status=%s",
                data.get("order_id"),
                data.get("payment_status"),
            )
        else:
            logger.debug(
                "[purchase:nowpayments_ipn] signature ok order_id=%s payment_status=%s",
                data.get("order_id"),
                data.get("payment_status"),
            )
        return ok
    except Exception:
        logger.exception("[purchase:nowpayments_ipn] signature verify error (parse/HMAC)")
        return False

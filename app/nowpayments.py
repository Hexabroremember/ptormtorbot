"""NOWPayments API client + IPN signature verification."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

import httpx

NOWPAYMENTS_BASE = "https://api.nowpayments.io/v1"


def _api_key() -> str:
    return os.environ.get("NOWPAYMENTS_API_KEY", "").strip()


def _ipn_secret() -> str:
    return os.environ.get("NOWPAYMENTS_IPN_SECRET", "").strip()


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
        raise ValueError("NOWPAYMENTS_API_KEY is not configured")

    payload = {
        "price_amount": price_amount,
        "price_currency": price_currency,
        "order_id": order_id,
        "order_description": order_description,
        "ipn_callback_url": ipn_callback_url,
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{NOWPAYMENTS_BASE}/invoice",
            json=payload,
            headers={"x-api-key": key, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


def verify_ipn_signature(raw_body: bytes, signature: str) -> bool:
    """
    Validate NOWPayments IPN request.
    Algorithm: sort JSON keys → HMAC-SHA512 with IPN secret.
    Returns True when no secret is configured (allows testing without it).
    """
    secret = _ipn_secret()
    if not secret:
        return True
    if not signature:
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
        return hmac.compare_digest(expected.lower(), signature.lower())
    except Exception:
        return False

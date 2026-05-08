"""Shared labels and metadata for payment codes (Telegram /admin issue + HTTP bulk issue)."""

from __future__ import annotations

from typing import Any

# Keys align with Mini App ``expiryOption`` / bot callback suffixes.
TIER_LABELS: dict[str, str] = {
    "300": "שנה · ₪300",
    "500": "3 שנים · ₪500",
    "900": "5 שנים · ₪900",
    "1200": "10 שנים · ₪1200",
    "1500": "לצמיתות · ₪1500",
}

VALID_TIER_KEYS: frozenset[str] = frozenset(TIER_LABELS.keys())
ALL_ISSUE_KEYS: frozenset[str] = frozenset({"global", *VALID_TIER_KEYS})

MAX_BULK_PER_KEY = 50
MAX_BULK_TOTAL = 100


def meta_for_issue_key(key: str) -> dict[str, Any]:
    """Build ``entry_json`` meta for ``issue_new_code`` — key is ``global`` or a tier id."""
    k = (key or "").strip().lower()
    if k == "global":
        return {
            "issue_scope": "global",
            "issue_label": "קוד גלובלי — כל תקופות התוקף",
        }
    if k in TIER_LABELS:
        return {
            "issue_scope": "tier",
            "expiry_option": k,
            "price_ils": float(k),
            "issue_label": TIER_LABELS[k],
        }
    raise ValueError(f"invalid_issue_key:{key!r}")


def heading_for_issue_key(key: str) -> str:
    """Short Hebrew heading for Telegram messages."""
    k = (key or "").strip().lower()
    if k == "global":
        return "קוד גלובלי — כל תקופות התוקף"
    if k in TIER_LABELS:
        return TIER_LABELS[k]
    return key


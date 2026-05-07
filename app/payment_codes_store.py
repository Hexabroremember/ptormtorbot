"""One-time payment approval codes: persisted JSON + in-process lock."""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR") or str(ROOT_DIR / "data"))
DATA_PATH = DATA_DIR / "payment_codes.json"

_lock = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_raw() -> dict[str, Any]:
    if not DATA_PATH.exists():
        return {"codes": {}}
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "codes" not in data:
            return {"codes": {}}
        if not isinstance(data["codes"], dict):
            data["codes"] = {}
        return data
    except (json.JSONDecodeError, OSError):
        return {"codes": {}}


def _atomic_write(obj: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_PATH.with_suffix(".json.tmp")
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(DATA_PATH)


def normalize_code(raw: str) -> str:
    return "".join(raw.split()).upper()


def issue_new_code() -> str:
    """Generate and persist a new unused code; retries on collision."""
    with _lock:
        for _ in range(50):
            code = secrets.token_hex(5).upper()
            data = _load_raw()
            codes = data.setdefault("codes", {})
            if code in codes:
                continue
            codes[code] = {"used": False, "created_at": _utc_now_iso()}
            _atomic_write(data)
            return code
    raise RuntimeError("Could not allocate unique payment code")


def list_codes(*, include_code: bool = False) -> list[dict[str, Any]]:
    """Return codes for admin views, newest first."""
    with _lock:
        data = _load_raw()
        codes = data.setdefault("codes", {})
        items: list[dict[str, Any]] = []
        for code, entry in codes.items():
            code_value = code if include_code else f"{code[:3]}…{code[-3:]}"
            row: dict[str, Any] = {
                "code": code_value,
                "used": bool(entry.get("used")),
                "created_at": entry.get("created_at"),
                "redeemed_at": entry.get("redeemed_at"),
            }
            red = entry.get("redemption")
            if isinstance(red, dict) and red:
                row["redemption"] = red
            items.append(row)
        items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return items


def codes_summary() -> dict[str, int]:
    with _lock:
        data = _load_raw()
        codes = data.setdefault("codes", {})
        total = len(codes)
        used = sum(1 for entry in codes.values() if entry.get("used"))
        return {"total": total, "used": used, "unused": total - used}


def redeem_code(raw: str, *, redemption: dict[str, Any] | None = None) -> tuple[bool, str]:
    """
    Consume one code. Returns (success, message_key).
    message_key: 'ok' | 'not_found' | 'already_used'

    ``redemption`` is persisted on the code record for admin (form snapshot + Telegram id).
    """
    code = normalize_code(raw)
    if len(code) < 8:
        return False, "not_found"

    with _lock:
        data = _load_raw()
        codes = data.setdefault("codes", {})
        entry = codes.get(code)
        if entry is None:
            return False, "not_found"
        if entry.get("used"):
            return False, "already_used"
        entry["used"] = True
        entry["redeemed_at"] = _utc_now_iso()
        if redemption:
            entry["redemption"] = redemption
        _atomic_write(data)
        return True, "ok"

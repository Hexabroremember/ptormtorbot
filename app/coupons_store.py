"""Discount coupon storage and validation."""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from app.storage_connection import connect_storage, qp, use_postgres

_lock = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_coupon_code(raw: str) -> str:
    return "".join(str(raw or "").split()).upper()


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _ensure_table(conn: Any) -> None:
    tg_type = "BIGINT" if use_postgres() else "INTEGER"
    value_type = "DOUBLE PRECISION" if use_postgres() else "REAL"
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS coupons (
            code TEXT PRIMARY KEY NOT NULL,
            discount_type TEXT NOT NULL,
            value {value_type} NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            max_uses INTEGER,
            used_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            telegram_user_id {tg_type},
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_coupons_active ON coupons(active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_coupons_user ON coupons(telegram_user_id)")


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row)


def create_coupon(
    *,
    code: str | None = None,
    discount_type: str,
    value: float,
    max_uses: int | None = None,
    expires_at: str | None = None,
    telegram_user_id: int | None = None,
    active: bool = True,
    note: str | None = None,
) -> dict[str, Any]:
    dtype = str(discount_type or "").strip().lower()
    if dtype not in {"percent", "fixed"}:
        raise ValueError("invalid_discount_type")
    amount = float(value)
    if dtype == "percent" and not (0 < amount <= 100):
        raise ValueError("invalid_percent")
    if dtype == "fixed" and amount <= 0:
        raise ValueError("invalid_fixed_amount")
    if max_uses is not None and max_uses <= 0:
        raise ValueError("invalid_max_uses")

    now = _utc_now_iso()
    with _lock, connect_storage() as conn:
        _ensure_table(conn)
        for _ in range(50):
            coupon_code = normalize_coupon_code(code or f"SAVE{secrets.token_hex(3)}")
            exists = conn.execute(qp("SELECT 1 FROM coupons WHERE code = ?"), (coupon_code,)).fetchone()
            if exists and not code:
                continue
            if exists:
                raise ValueError("coupon_exists")
            conn.execute(
                qp(
                    """
                    INSERT INTO coupons
                        (code, discount_type, value, active, max_uses, used_count,
                         expires_at, telegram_user_id, note, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    coupon_code,
                    dtype,
                    amount,
                    1 if active else 0,
                    max_uses,
                    expires_at,
                    telegram_user_id,
                    note,
                    now,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(qp("SELECT * FROM coupons WHERE code = ?"), (coupon_code,)).fetchone()
            return _row_to_dict(row)
    raise RuntimeError("could_not_create_coupon")


def list_coupons() -> dict[str, Any]:
    with _lock, connect_storage() as conn:
        _ensure_table(conn)
        rows = conn.execute("SELECT * FROM coupons ORDER BY created_at DESC").fetchall()
    return {"items": [_row_to_dict(row) for row in rows]}


def set_coupon_active(code: str, active: bool) -> dict[str, bool]:
    norm = normalize_coupon_code(code)
    now = _utc_now_iso()
    with _lock, connect_storage() as conn:
        _ensure_table(conn)
        cur = conn.execute(
            qp("UPDATE coupons SET active = ?, updated_at = ? WHERE code = ?"),
            (1 if active else 0, now, norm),
        )
        conn.commit()
    return {"ok": cur.rowcount > 0}


def validate_coupon(
    raw_code: str,
    *,
    original_price_ils: float,
    telegram_user_id: int | None = None,
) -> dict[str, Any]:
    code = normalize_coupon_code(raw_code)
    if not code:
        return {
            "ok": False,
            "reason": "empty_coupon",
            "discount_ils": 0.0,
            "final_price_ils": float(original_price_ils),
        }

    with _lock, connect_storage() as conn:
        _ensure_table(conn)
        row = conn.execute(qp("SELECT * FROM coupons WHERE code = ?"), (code,)).fetchone()
    if row is None:
        return {"ok": False, "reason": "coupon_not_found", "discount_ils": 0.0, "final_price_ils": float(original_price_ils)}
    item = _row_to_dict(row)
    if not int(item.get("active") or 0):
        return {"ok": False, "reason": "coupon_inactive", "coupon": item, "discount_ils": 0.0, "final_price_ils": float(original_price_ils)}
    exp = _parse_dt(item.get("expires_at"))
    if exp and exp < datetime.now(timezone.utc):
        return {"ok": False, "reason": "coupon_expired", "coupon": item, "discount_ils": 0.0, "final_price_ils": float(original_price_ils)}
    max_uses = item.get("max_uses")
    if max_uses is not None and int(item.get("used_count") or 0) >= int(max_uses):
        return {"ok": False, "reason": "coupon_exhausted", "coupon": item, "discount_ils": 0.0, "final_price_ils": float(original_price_ils)}
    scoped_user = item.get("telegram_user_id")
    if scoped_user is not None and telegram_user_id is not None and int(scoped_user) != int(telegram_user_id):
        return {"ok": False, "reason": "coupon_wrong_user", "coupon": item, "discount_ils": 0.0, "final_price_ils": float(original_price_ils)}
    if scoped_user is not None and telegram_user_id is None:
        return {"ok": False, "reason": "telegram_user_required", "coupon": item, "discount_ils": 0.0, "final_price_ils": float(original_price_ils)}

    original = max(0.0, float(original_price_ils))
    if item["discount_type"] == "percent":
        discount = original * (float(item["value"]) / 100.0)
    else:
        discount = float(item["value"])
    discount = min(original, max(0.0, round(discount, 2)))
    final = max(0.0, round(original - discount, 2))
    return {
        "ok": True,
        "coupon": item,
        "discount_ils": discount,
        "final_price_ils": final,
    }


def record_coupon_use(code: str) -> None:
    norm = normalize_coupon_code(code)
    if not norm:
        return
    now = _utc_now_iso()
    with _lock, connect_storage() as conn:
        _ensure_table(conn)
        conn.execute(
            qp("UPDATE coupons SET used_count = used_count + 1, updated_at = ? WHERE code = ?"),
            (now, norm),
        )
        conn.commit()


def summary() -> dict[str, int]:
    with _lock, connect_storage() as conn:
        _ensure_table(conn)
        rows = conn.execute("SELECT active, used_count FROM coupons").fetchall()
    total = len(rows)
    active = sum(1 for r in rows if int(r["active"] or 0))
    used = sum(int(r["used_count"] or 0) for r in rows)
    return {"total": total, "active": active, "used": used}

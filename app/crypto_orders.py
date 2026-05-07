"""SQLite-backed NOWPayments order tracking."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR") or str(ROOT_DIR / "data"))
DB_PATH = DATA_DIR / "events.sqlite3"

_lock = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_orders_table() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crypto_orders (
                order_id TEXT PRIMARY KEY,
                telegram_user_id INTEGER,
                username TEXT,
                first_name TEXT,
                price_ils REAL NOT NULL,
                expiry_option TEXT,
                invoice_url TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                payment_code TEXT,
                form_json TEXT,
                pdf_sent_to_telegram INTEGER NOT NULL DEFAULT 0,
                ipn_payload TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(crypto_orders)").fetchall()
        }
        if "form_json" not in existing:
            conn.execute("ALTER TABLE crypto_orders ADD COLUMN form_json TEXT")
        if "pdf_sent_to_telegram" not in existing:
            conn.execute(
                "ALTER TABLE crypto_orders ADD COLUMN pdf_sent_to_telegram INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_crypto_orders_tg ON crypto_orders(telegram_user_id)"
        )


def create_order(
    *,
    order_id: str,
    telegram_user_id: int | None,
    username: str | None,
    first_name: str | None,
    price_ils: float,
    expiry_option: str | None,
    invoice_url: str,
    form: dict[str, Any] | None = None,
) -> None:
    init_orders_table()
    now = _utc_now()
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO crypto_orders
                (order_id, telegram_user_id, username, first_name,
                 price_ils, expiry_option, invoice_url,
                 status, payment_code, form_json, pdf_sent_to_telegram, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, 0, ?, ?)
            """,
            (
                order_id,
                telegram_user_id,
                username,
                first_name,
                price_ils,
                expiry_option,
                invoice_url,
                json.dumps(form, ensure_ascii=False) if form else None,
                now,
                now,
            ),
        )


def mark_paid(*, order_id: str, payment_code: str, ipn_payload: dict[str, Any]) -> bool:
    """Mark order paid and store the issued payment code. Returns True if a row was updated."""
    init_orders_table()
    now = _utc_now()
    with _lock, _connect() as conn:
        cur = conn.execute(
            """
            UPDATE crypto_orders
            SET status = 'paid', payment_code = ?, ipn_payload = ?, updated_at = ?
            WHERE order_id = ? AND status != 'paid'
            """,
            (payment_code, json.dumps(ipn_payload, ensure_ascii=False), now, order_id),
        )
        return cur.rowcount > 0


def get_order(order_id: str) -> dict[str, Any] | None:
    init_orders_table()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM crypto_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
    if row is None:
        return None
    out = dict(row)
    form_raw = out.get("form_json")
    if isinstance(form_raw, str) and form_raw:
        try:
            out["form"] = json.loads(form_raw)
        except json.JSONDecodeError:
            out["form"] = None
    return out


def mark_pdf_sent(order_id: str) -> None:
    init_orders_table()
    now = _utc_now()
    with _lock, _connect() as conn:
        conn.execute(
            """
            UPDATE crypto_orders
            SET pdf_sent_to_telegram = 1, updated_at = ?
            WHERE order_id = ?
            """,
            (now, order_id),
        )


def list_orders(*, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Admin view: all crypto orders newest-first."""
    init_orders_table()
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM crypto_orders").fetchone()["n"]
        rows = conn.execute(
            "SELECT * FROM crypto_orders ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}

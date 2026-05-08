"""NOWPayments order tracking — SQLite or PostgreSQL."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from app.storage_connection import connect_storage, qp, use_postgres

_lock = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_columns_sqlite(conn: Any, table: str) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _table_columns_pg(conn: Any, table: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    ).fetchall()
    return {r["column_name"] for r in rows}


def init_orders_table() -> None:
    with _lock, connect_storage() as conn:
        if use_postgres():
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS crypto_orders (
                    order_id TEXT PRIMARY KEY,
                    telegram_user_id BIGINT,
                    username TEXT,
                    first_name TEXT,
                    price_ils DOUBLE PRECISION NOT NULL,
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
            existing = _table_columns_pg(conn, "crypto_orders")
        else:
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
            existing = _table_columns_sqlite(conn, "crypto_orders")
        if "form_json" not in existing:
            conn.execute("ALTER TABLE crypto_orders ADD COLUMN form_json TEXT")
        if "pdf_sent_to_telegram" not in existing:
            conn.execute(
                "ALTER TABLE crypto_orders ADD COLUMN pdf_sent_to_telegram INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_crypto_orders_tg ON crypto_orders(telegram_user_id)"
        )
        conn.commit()


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
    form_json = json.dumps(form, ensure_ascii=False) if form else None
    with _lock, connect_storage() as conn:
        if use_postgres():
            conn.execute(
                """
                INSERT INTO crypto_orders
                    (order_id, telegram_user_id, username, first_name,
                     price_ils, expiry_option, invoice_url,
                     status, payment_code, form_json, pdf_sent_to_telegram, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', NULL, %s, 0, %s, %s)
                ON CONFLICT (order_id) DO UPDATE SET
                    telegram_user_id = EXCLUDED.telegram_user_id,
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    price_ils = EXCLUDED.price_ils,
                    expiry_option = EXCLUDED.expiry_option,
                    invoice_url = EXCLUDED.invoice_url,
                    status = EXCLUDED.status,
                    payment_code = EXCLUDED.payment_code,
                    form_json = EXCLUDED.form_json,
                    pdf_sent_to_telegram = EXCLUDED.pdf_sent_to_telegram,
                    ipn_payload = EXCLUDED.ipn_payload,
                    created_at = EXCLUDED.created_at,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    order_id,
                    telegram_user_id,
                    username,
                    first_name,
                    price_ils,
                    expiry_option,
                    invoice_url,
                    form_json,
                    now,
                    now,
                ),
            )
        else:
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
                    form_json,
                    now,
                    now,
                ),
            )
        conn.commit()


def mark_paid(*, order_id: str, payment_code: str, ipn_payload: dict[str, Any]) -> bool:
    init_orders_table()
    now = _utc_now()
    with _lock, connect_storage() as conn:
        cur = conn.execute(
            qp(
                """
                UPDATE crypto_orders
                SET status = 'paid', payment_code = ?, ipn_payload = ?, updated_at = ?
                WHERE order_id = ? AND status != 'paid'
                """
            ),
            (payment_code, json.dumps(ipn_payload, ensure_ascii=False), now, order_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_order(order_id: str) -> dict[str, Any] | None:
    init_orders_table()
    with connect_storage() as conn:
        row = conn.execute(
            qp("SELECT * FROM crypto_orders WHERE order_id = ?"),
            (order_id,),
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
    with _lock, connect_storage() as conn:
        conn.execute(
            qp(
                """
                UPDATE crypto_orders
                SET pdf_sent_to_telegram = 1, updated_at = ?
                WHERE order_id = ?
                """
            ),
            (now, order_id),
        )
        conn.commit()


def list_paid_orders_for_user(telegram_user_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
    """Paid crypto orders for replay PDF download."""
    init_orders_table()
    limit = max(1, min(limit, 100))
    with connect_storage() as conn:
        rows = conn.execute(
            qp(
                """
                SELECT order_id, created_at, updated_at, price_ils, expiry_option,
                       payment_code, form_json
                FROM crypto_orders
                WHERE telegram_user_id = ?
                  AND status = 'paid'
                ORDER BY updated_at DESC
                LIMIT ?
                """
            ),
            (telegram_user_id, limit),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        form: dict[str, Any] | None = None
        fj = d.get("form_json")
        if isinstance(fj, str) and fj.strip():
            try:
                parsed = json.loads(fj)
                form = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                form = None
        out.append(
            {
                "order_id": d["order_id"],
                "ts": d.get("updated_at") or d.get("created_at"),
                "price_ils": d.get("price_ils"),
                "expiry_option": d.get("expiry_option"),
                "payment_code": d.get("payment_code"),
                "form": form,
            }
        )
    return out


def list_orders(*, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    init_orders_table()
    with connect_storage() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM crypto_orders").fetchone()["n"]
        rows = conn.execute(
            qp("SELECT * FROM crypto_orders ORDER BY created_at DESC LIMIT ? OFFSET ?"),
            (limit, offset),
        ).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}

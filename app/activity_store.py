"""Persistent activity/event storage for admin analytics."""

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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                telegram_user_id INTEGER,
                username TEXT,
                first_name TEXT,
                meta_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_user ON events(telegram_user_id)")


def log_event(
    event_type: str,
    *,
    source: str,
    telegram_user_id: int | None = None,
    username: str | None = None,
    first_name: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    init_db()
    payload = json.dumps(meta or {}, ensure_ascii=False, separators=(",", ":"))
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO events (ts, event_type, source, telegram_user_id, username, first_name, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (utc_now_iso(), event_type, source, telegram_user_id, username, first_name, payload),
        )


def list_events(
    *,
    limit: int = 100,
    offset: int = 0,
    event_type: str | None = None,
    telegram_user_id: int | None = None,
) -> dict[str, Any]:
    init_db()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    where: list[str] = []
    params: list[Any] = []
    if event_type:
        where.append("event_type = ?")
        params.append(event_type)
    if telegram_user_id is not None:
        where.append("telegram_user_id = ?")
        params.append(telegram_user_id)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM events {where_sql}",
            params,
        ).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT id, ts, event_type, source, telegram_user_id, username, first_name, meta_json
            FROM events
            {where_sql}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()

    return {
        "total": total,
        "items": [_event_row_to_dict(row) for row in rows],
    }


def summary() -> dict[str, Any]:
    init_db()
    with _connect() as conn:
        total_events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        unique_users = conn.execute(
            "SELECT COUNT(DISTINCT telegram_user_id) AS n FROM events WHERE telegram_user_id IS NOT NULL"
        ).fetchone()["n"]
        by_type = [
            dict(row)
            for row in conn.execute(
                """
                SELECT event_type, COUNT(*) AS count
                FROM events
                GROUP BY event_type
                ORDER BY count DESC, event_type ASC
                """
            ).fetchall()
        ]
        by_day = [
            dict(row)
            for row in conn.execute(
                """
                SELECT substr(ts, 1, 10) AS day, COUNT(*) AS count
                FROM events
                GROUP BY day
                ORDER BY day DESC
                LIMIT 14
                """
            ).fetchall()
        ]
        recent = [
            _event_row_to_dict(row)
            for row in conn.execute(
                """
                SELECT id, ts, event_type, source, telegram_user_id, username, first_name, meta_json
                FROM events
                ORDER BY id DESC
                LIMIT 10
                """
            ).fetchall()
        ]
        redeem_row = conn.execute(
            """
            SELECT
              COUNT(*) AS total_redemptions,
              COUNT(DISTINCT telegram_user_id) AS distinct_redeemers
            FROM events
            WHERE event_type = 'payment_code_redeemed'
            """
        ).fetchone()
        redeem_stats = {
            "total_redemptions": redeem_row["total_redemptions"] if redeem_row else 0,
            "distinct_redeemers": redeem_row["distinct_redeemers"] if redeem_row else 0,
        }
    return {
        "total_events": total_events,
        "unique_users": unique_users,
        "by_type": by_type,
        "by_day": list(reversed(by_day)),
        "recent": recent,
        "redeem_stats": redeem_stats,
    }


def list_user_directory(*, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Aggregated per Telegram user for admin directory."""
    init_db()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with _connect() as conn:
        total = conn.execute(
            "SELECT COUNT(DISTINCT telegram_user_id) AS n FROM events WHERE telegram_user_id IS NOT NULL"
        ).fetchone()["n"]
        rows = conn.execute(
            """
            SELECT
              telegram_user_id,
              MAX(username) AS username,
              MAX(first_name) AS first_name,
              COUNT(*) AS event_count,
              MAX(ts) AS last_seen_ts,
              SUM(CASE WHEN event_type = 'payment_code_redeemed' THEN 1 ELSE 0 END) AS redeem_count,
              SUM(CASE WHEN event_type = 'pdf_generated' THEN 1 ELSE 0 END) AS pdf_generated_count,
              SUM(CASE WHEN event_type = 'pdf_downloaded' THEN 1 ELSE 0 END) AS pdf_download_count,
              SUM(CASE WHEN event_type LIKE 'bot_%' THEN 1 ELSE 0 END) AS bot_events_count
            FROM events
            WHERE telegram_user_id IS NOT NULL
            GROUP BY telegram_user_id
            ORDER BY last_seen_ts DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

    items = []
    for row in rows:
        items.append(
            {
                "telegram_user_id": row["telegram_user_id"],
                "username": row["username"],
                "first_name": row["first_name"],
                "event_count": row["event_count"],
                "last_seen_ts": row["last_seen_ts"],
                "redeem_count": row["redeem_count"] or 0,
                "pdf_generated_count": row["pdf_generated_count"] or 0,
                "pdf_download_count": row["pdf_download_count"] or 0,
                "bot_events_count": row["bot_events_count"] or 0,
            }
        )
    return {"total": total, "items": items}


def _event_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        meta = json.loads(row["meta_json"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    return {
        "id": row["id"],
        "ts": row["ts"],
        "event_type": row["event_type"],
        "source": row["source"],
        "telegram_user_id": row["telegram_user_id"],
        "username": row["username"],
        "first_name": row["first_name"],
        "meta": meta,
    }

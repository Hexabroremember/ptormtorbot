"""SQLite-backed per-user rate limits and admin overrides."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from app.admin_auth import TelegramWebAppUser, admin_ids

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR") or str(ROOT_DIR / "data"))
DB_PATH = DATA_DIR / "events.sqlite3"

_lock = threading.Lock()


DEFAULT_LIMITS: dict[str, tuple[int, int]] = {
    "preview_pdf": (int(os.environ.get("RATE_PREVIEW_PER_HOUR", "20")), 60 * 60),
    "final_pdf": (int(os.environ.get("RATE_FINAL_PER_DAY", "8")), 24 * 60 * 60),
    "create_invoice": (int(os.environ.get("RATE_INVOICE_PER_HOUR", "6")), 60 * 60),
    "redeem": (int(os.environ.get("RATE_REDEEM_PER_HOUR", "10")), 60 * 60),
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_counters (
                identity_key TEXT NOT NULL,
                limit_key TEXT NOT NULL,
                window_start INTEGER NOT NULL,
                count INTEGER NOT NULL,
                PRIMARY KEY (identity_key, limit_key, window_start)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_limit_overrides (
                telegram_user_id INTEGER PRIMARY KEY,
                expires_at TEXT,
                bypass INTEGER NOT NULL DEFAULT 1,
                multiplier REAL NOT NULL DEFAULT 2.0,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def _identity_for(request: Request, tg_user: TelegramWebAppUser | None) -> tuple[str, int | None]:
    if tg_user:
        return f"tg:{tg_user.id}", tg_user.id
    client = request.client.host if request.client else "unknown"
    return f"ip:{client}", None


def _active_override(conn: sqlite3.Connection, telegram_user_id: int | None) -> dict[str, Any] | None:
    if telegram_user_id is None:
        return None
    row = conn.execute(
        "SELECT * FROM rate_limit_overrides WHERE telegram_user_id = ?",
        (telegram_user_id,),
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    exp = out.get("expires_at")
    if exp:
        try:
            if datetime.fromisoformat(str(exp)).timestamp() < time.time():
                return None
        except ValueError:
            return None
    return out


def check_rate_limit(limit_key: str, request: Request, tg_user: TelegramWebAppUser | None) -> None:
    """Consume one unit or raise HTTP 429."""
    init_db()
    base_limit, window_seconds = DEFAULT_LIMITS.get(limit_key, (20, 60 * 60))
    if base_limit <= 0:
        return

    identity_key, telegram_user_id = _identity_for(request, tg_user)
    if telegram_user_id in admin_ids():
        return

    now = int(time.time())
    window_start = now - (now % window_seconds)

    with _lock, _connect() as conn:
        override = _active_override(conn, telegram_user_id)
        if override:
            if int(override.get("bypass") or 0):
                return
            multiplier = max(1.0, float(override.get("multiplier") or 1.0))
            limit = max(1, int(base_limit * multiplier))
        else:
            limit = base_limit

        row = conn.execute(
            """
            SELECT count FROM rate_counters
            WHERE identity_key = ? AND limit_key = ? AND window_start = ?
            """,
            (identity_key, limit_key, window_start),
        ).fetchone()
        current = int(row["count"]) if row else 0
        if current >= limit:
            retry_after = max(1, window_start + window_seconds - now)
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "rate_limited",
                    "limit_key": limit_key,
                    "retry_after_sec": retry_after,
                },
            )
        if row:
            conn.execute(
                """
                UPDATE rate_counters
                SET count = count + 1
                WHERE identity_key = ? AND limit_key = ? AND window_start = ?
                """,
                (identity_key, limit_key, window_start),
            )
        else:
            conn.execute(
                """
                INSERT INTO rate_counters (identity_key, limit_key, window_start, count)
                VALUES (?, ?, ?, 1)
                """,
                (identity_key, limit_key, window_start),
            )


def list_overrides() -> dict[str, Any]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT telegram_user_id, expires_at, bypass, multiplier, notes, created_at, updated_at
            FROM rate_limit_overrides
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


def upsert_override(
    *,
    telegram_user_id: int,
    expires_at: str | None,
    bypass: bool,
    multiplier: float,
    notes: str | None,
) -> dict[str, Any]:
    init_db()
    now = utc_now_iso()
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO rate_limit_overrides
                (telegram_user_id, expires_at, bypass, multiplier, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                expires_at = excluded.expires_at,
                bypass = excluded.bypass,
                multiplier = excluded.multiplier,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (
                telegram_user_id,
                expires_at,
                1 if bypass else 0,
                max(1.0, multiplier),
                notes,
                now,
                now,
            ),
        )
    return {
        "telegram_user_id": telegram_user_id,
        "expires_at": expires_at,
        "bypass": bypass,
        "multiplier": max(1.0, multiplier),
        "notes": notes,
    }


def delete_override(telegram_user_id: int) -> dict[str, bool]:
    init_db()
    with _lock, _connect() as conn:
        conn.execute(
            "DELETE FROM rate_limit_overrides WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
    return {"ok": True}

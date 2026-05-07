"""Short-lived PDF bytes keyed by token — SQLite-backed so Telegram can fetch across workers.

Without persistence, each Uvicorn worker / replica holds tokens only in RAM; Telegram's
servers fetch ``GET /pdf-download/{token}`` and may hit a different process → 404 → no file in chat.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR") or str(ROOT_DIR / "data"))
DB_PATH = DATA_DIR / "events.sqlite3"

TTL_SECONDS = 900  # 15 minutes

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pdf_download_tokens (
            token TEXT PRIMARY KEY NOT NULL,
            pdf_blob BLOB NOT NULL,
            expires_at REAL NOT NULL,
            telegram_user_id INTEGER,
            username TEXT,
            first_name TEXT
        )
        """
    )
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(pdf_download_tokens)").fetchall()
    }
    for col, ddl in (
        ("telegram_user_id", "ALTER TABLE pdf_download_tokens ADD COLUMN telegram_user_id INTEGER"),
        ("username", "ALTER TABLE pdf_download_tokens ADD COLUMN username TEXT"),
        ("first_name", "ALTER TABLE pdf_download_tokens ADD COLUMN first_name TEXT"),
    ):
        if col not in existing:
            conn.execute(ddl)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdf_dl_expires ON pdf_download_tokens(expires_at)"
    )


def _purge_expired(conn: sqlite3.Connection, now: float) -> None:
    conn.execute("DELETE FROM pdf_download_tokens WHERE expires_at <= ?", (now,))


def register_pdf_bytes(data: bytes, *, user_meta: dict[str, Any] | None = None) -> str:
    token = secrets.token_urlsafe(24)
    now = time.time()
    expires_at = now + TTL_SECONDS
    user_meta = user_meta or {}
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            _purge_expired(conn, now)
            conn.execute(
                """
                INSERT INTO pdf_download_tokens
                    (token, pdf_blob, expires_at, telegram_user_id, username, first_name)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    data,
                    expires_at,
                    user_meta.get("telegram_user_id"),
                    user_meta.get("username"),
                    user_meta.get("first_name"),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return token


def get_pdf_record(token: str) -> dict[str, Any] | None:
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            _purge_expired(conn, now)
            row = conn.execute(
                """
                SELECT pdf_blob, expires_at, telegram_user_id, username, first_name
                FROM pdf_download_tokens
                WHERE token = ?
                """,
                (token,),
            ).fetchone()
            if row is None:
                return None
            blob, exp, telegram_user_id, username, first_name = row
            if exp <= now:
                conn.execute("DELETE FROM pdf_download_tokens WHERE token = ?", (token,))
                conn.commit()
                return None
            return {
                "pdf_blob": blob,
                "telegram_user_id": telegram_user_id,
                "username": username,
                "first_name": first_name,
            }
        finally:
            conn.close()


def get_pdf_bytes(token: str) -> bytes | None:
    record = get_pdf_record(token)
    if record is None:
        return None
    return record["pdf_blob"]

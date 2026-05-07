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
            expires_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pdf_dl_expires ON pdf_download_tokens(expires_at)"
    )


def _purge_expired(conn: sqlite3.Connection, now: float) -> None:
    conn.execute("DELETE FROM pdf_download_tokens WHERE expires_at <= ?", (now,))


def register_pdf_bytes(data: bytes) -> str:
    token = secrets.token_urlsafe(24)
    now = time.time()
    expires_at = now + TTL_SECONDS
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            _purge_expired(conn, now)
            conn.execute(
                """
                INSERT INTO pdf_download_tokens (token, pdf_blob, expires_at)
                VALUES (?, ?, ?)
                """,
                (token, data, expires_at),
            )
            conn.commit()
        finally:
            conn.close()
    return token


def get_pdf_bytes(token: str) -> bytes | None:
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            _purge_expired(conn, now)
            row = conn.execute(
                "SELECT pdf_blob, expires_at FROM pdf_download_tokens WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            blob, exp = row
            if exp <= now:
                conn.execute("DELETE FROM pdf_download_tokens WHERE token = ?", (token,))
                conn.commit()
                return None
            return blob
        finally:
            conn.close()

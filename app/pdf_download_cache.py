"""Short-lived PDF bytes keyed by token — persisted so Telegram can fetch across workers."""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from app.storage_connection import connect_storage, qp, use_postgres

TTL_SECONDS = 900  # 15 minutes

_lock = threading.Lock()


def _table_columns_pg(conn: Any, table: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    ).fetchall()
    return {r["column_name"] for r in rows}


def _ensure_schema(conn: Any) -> None:
    if use_postgres():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pdf_download_tokens (
                token TEXT PRIMARY KEY NOT NULL,
                pdf_blob BYTEA NOT NULL,
                expires_at DOUBLE PRECISION NOT NULL,
                telegram_user_id BIGINT,
                username TEXT,
                first_name TEXT
            )
            """
        )
        existing = _table_columns_pg(conn, "pdf_download_tokens")
        if "telegram_user_id" not in existing:
            conn.execute("ALTER TABLE pdf_download_tokens ADD COLUMN telegram_user_id BIGINT")
        if "username" not in existing:
            conn.execute("ALTER TABLE pdf_download_tokens ADD COLUMN username TEXT")
        if "first_name" not in existing:
            conn.execute("ALTER TABLE pdf_download_tokens ADD COLUMN first_name TEXT")
    else:
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


def _purge_expired(conn: Any, now: float) -> None:
    conn.execute(qp("DELETE FROM pdf_download_tokens WHERE expires_at <= ?"), (now,))


def register_pdf_bytes(data: bytes, *, user_meta: dict[str, Any] | None = None) -> str:
    token = secrets.token_urlsafe(24)
    now = time.time()
    expires_at = now + TTL_SECONDS
    user_meta = user_meta or {}
    with _lock:
        conn = connect_storage()
        try:
            _ensure_schema(conn)
            _purge_expired(conn, now)
            conn.execute(
                qp(
                    """
                    INSERT INTO pdf_download_tokens
                        (token, pdf_blob, expires_at, telegram_user_id, username, first_name)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """
                ),
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
        conn = connect_storage()
        try:
            _ensure_schema(conn)
            _purge_expired(conn, now)
            row = conn.execute(
                qp(
                    """
                    SELECT pdf_blob, expires_at, telegram_user_id, username, first_name
                    FROM pdf_download_tokens
                    WHERE token = ?
                    """
                ),
                (token,),
            ).fetchone()
            if row is None:
                return None
            blob = row["pdf_blob"]
            exp = row["expires_at"]
            telegram_user_id = row["telegram_user_id"]
            username = row["username"]
            first_name = row["first_name"]
            if exp <= now:
                conn.execute(qp("DELETE FROM pdf_download_tokens WHERE token = ?"), (token,))
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

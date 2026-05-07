"""Saved Mini App form snapshots per Telegram user — SQLite or PostgreSQL."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.storage_connection import connect_storage, qp, use_postgres

MAX_FORMS_PER_USER = int(os.environ.get("MAX_SAVED_FORMS_PER_USER", "15"))

_lock = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_schema(conn: Any) -> None:
    if use_postgres():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_saved_forms (
                id TEXT PRIMARY KEY NOT NULL,
                telegram_user_id BIGINT NOT NULL,
                title TEXT,
                form_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_saved_forms (
                id TEXT PRIMARY KEY NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                title TEXT,
                form_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_saved_forms_user_updated "
        "ON user_saved_forms(telegram_user_id, updated_at DESC)"
    )


def _clean_form(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "fullName",
        "fullNameEn",
        "idNumber",
        "expiryOption",
        "birthDate",
        "idIssueDate",
    }
    out: dict[str, Any] = {}
    for key in allowed:
        val = raw.get(key)
        if val is None:
            out[key] = ""
        elif isinstance(val, str):
            out[key] = val.strip()
        else:
            out[key] = str(val).strip()
    return out


def _title_from_form(form: dict[str, Any]) -> str:
    title = str(form.get("fullName") or form.get("fullNameEn") or "").strip()
    return title[:80] or "טופס שמור"


def _row_to_dict(row: Any) -> dict[str, Any]:
    try:
        form = json.loads(row["form_json"] or "{}")
    except json.JSONDecodeError:
        form = {}
    return {
        "id": row["id"],
        "telegram_user_id": row["telegram_user_id"],
        "title": row["title"] or _title_from_form(form),
        "form": form if isinstance(form, dict) else {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_for_user(telegram_user_id: int, *, limit: int = 15) -> dict[str, Any]:
    limit = max(1, min(limit, 50))
    with _lock:
        conn = connect_storage()
        try:
            _ensure_schema(conn)
            rows = conn.execute(
                qp(
                    """
                    SELECT id, telegram_user_id, title, form_json, created_at, updated_at
                    FROM user_saved_forms
                    WHERE telegram_user_id = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """
                ),
                (telegram_user_id, limit),
            ).fetchall()
        finally:
            conn.close()
    return {"items": [_row_to_dict(row) for row in rows]}


def upsert_for_user(
    telegram_user_id: int,
    *,
    form: dict[str, Any],
    form_id: str | None = None,
) -> dict[str, Any]:
    clean = _clean_form(form)
    title = _title_from_form(clean)
    now = _utc_now_iso()
    row_id = (form_id or "").strip() or str(uuid.uuid4())

    with _lock:
        conn = connect_storage()
        try:
            _ensure_schema(conn)
            existing = conn.execute(
                qp(
                    """
                    SELECT id, created_at
                    FROM user_saved_forms
                    WHERE id = ? AND telegram_user_id = ?
                    """
                ),
                (row_id, telegram_user_id),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            form_payload = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
            if use_postgres():
                conn.execute(
                    """
                    INSERT INTO user_saved_forms
                        (id, telegram_user_id, title, form_json, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        telegram_user_id = EXCLUDED.telegram_user_id,
                        title = EXCLUDED.title,
                        form_json = EXCLUDED.form_json,
                        created_at = EXCLUDED.created_at,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (row_id, telegram_user_id, title, form_payload, created_at, now),
                )
            else:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO user_saved_forms
                        (id, telegram_user_id, title, form_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (row_id, telegram_user_id, title, form_payload, created_at, now),
                )
            _trim_for_user(conn, telegram_user_id)
            conn.commit()
            row = conn.execute(
                qp(
                    """
                    SELECT id, telegram_user_id, title, form_json, created_at, updated_at
                    FROM user_saved_forms
                    WHERE id = ? AND telegram_user_id = ?
                    """
                ),
                (row_id, telegram_user_id),
            ).fetchone()
        finally:
            conn.close()
    return _row_to_dict(row)


def delete_for_user(telegram_user_id: int, form_id: str) -> dict[str, bool]:
    with _lock:
        conn = connect_storage()
        try:
            _ensure_schema(conn)
            cur = conn.execute(
                qp(
                    """
                    DELETE FROM user_saved_forms
                    WHERE id = ? AND telegram_user_id = ?
                    """
                ),
                (form_id, telegram_user_id),
            )
            conn.commit()
            return {"ok": cur.rowcount > 0}
        finally:
            conn.close()


def _trim_for_user(conn: Any, telegram_user_id: int) -> None:
    keep = max(1, MAX_FORMS_PER_USER)
    conn.execute(
        qp(
            """
            DELETE FROM user_saved_forms
            WHERE telegram_user_id = ?
              AND id NOT IN (
                SELECT id
                FROM user_saved_forms
                WHERE telegram_user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
              )
            """
        ),
        (telegram_user_id, telegram_user_id, keep),
    )

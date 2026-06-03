"""Persistent Telegram user directory and broadcast delivery state."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from app.storage_connection import connect_storage, qp, use_postgres
from app.telegram_notify import send_telegram_message

_lock = threading.Lock()
_schema_initialized = False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    with _lock, connect_storage() as conn:
        if use_postgres():
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_users (
                    telegram_user_id BIGINT PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    language_code TEXT,
                    is_bot BOOLEAN NOT NULL DEFAULT FALSE,
                    source TEXT NOT NULL DEFAULT 'unknown',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_interaction_event TEXT,
                    can_broadcast BOOLEAN NOT NULL DEFAULT TRUE,
                    blocked_at TEXT,
                    last_broadcast_at TEXT,
                    broadcast_success_count INTEGER NOT NULL DEFAULT 0,
                    broadcast_failure_count INTEGER NOT NULL DEFAULT 0,
                    last_broadcast_error TEXT
                )
                """
            )
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_users (
                    telegram_user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    language_code TEXT,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'unknown',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_interaction_event TEXT,
                    can_broadcast INTEGER NOT NULL DEFAULT 1,
                    blocked_at TEXT,
                    last_broadcast_at TEXT,
                    broadcast_success_count INTEGER NOT NULL DEFAULT 0,
                    broadcast_failure_count INTEGER NOT NULL DEFAULT 0,
                    last_broadcast_error TEXT
                )
                """
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_telegram_users_last_seen ON telegram_users(last_seen_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_telegram_users_can_broadcast ON telegram_users(can_broadcast)")
        conn.commit()
    _schema_initialized = True


def _clean_text(value: Any, max_len: int = 256) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s[:max_len] if s else None


def upsert_telegram_user(
    telegram_user_id: int | None,
    *,
    chat_id: int | None = None,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    language_code: str | None = None,
    is_bot: bool | None = None,
    source: str = "unknown",
    event_type: str | None = None,
) -> None:
    """Create or refresh a Telegram user row without disturbing broadcast block state."""
    if telegram_user_id is None:
        return
    try:
        uid = int(telegram_user_id)
    except (TypeError, ValueError):
        return
    cid = int(chat_id) if chat_id is not None else uid
    now = utc_now_iso()
    init_db()
    if use_postgres():
        sql = """
            INSERT INTO telegram_users (
                telegram_user_id, chat_id, username, first_name, last_name, language_code,
                is_bot, source, first_seen_at, last_seen_at, last_interaction_event
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (telegram_user_id) DO UPDATE SET
                chat_id = EXCLUDED.chat_id,
                username = COALESCE(EXCLUDED.username, telegram_users.username),
                first_name = COALESCE(EXCLUDED.first_name, telegram_users.first_name),
                last_name = COALESCE(EXCLUDED.last_name, telegram_users.last_name),
                language_code = COALESCE(EXCLUDED.language_code, telegram_users.language_code),
                is_bot = EXCLUDED.is_bot,
                source = EXCLUDED.source,
                last_seen_at = EXCLUDED.last_seen_at,
                last_interaction_event = EXCLUDED.last_interaction_event
        """
    else:
        sql = """
            INSERT INTO telegram_users (
                telegram_user_id, chat_id, username, first_name, last_name, language_code,
                is_bot, source, first_seen_at, last_seen_at, last_interaction_event
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                username = COALESCE(excluded.username, telegram_users.username),
                first_name = COALESCE(excluded.first_name, telegram_users.first_name),
                last_name = COALESCE(excluded.last_name, telegram_users.last_name),
                language_code = COALESCE(excluded.language_code, telegram_users.language_code),
                is_bot = excluded.is_bot,
                source = excluded.source,
                last_seen_at = excluded.last_seen_at,
                last_interaction_event = excluded.last_interaction_event
        """
    with _lock, connect_storage() as conn:
        conn.execute(
            sql,
            (
                uid,
                cid,
                _clean_text(username, 128),
                _clean_text(first_name, 128),
                _clean_text(last_name, 128),
                _clean_text(language_code, 16),
                bool(is_bot) if is_bot is not None else False,
                _clean_text(source, 80) or "unknown",
                now,
                now,
                _clean_text(event_type, 80),
            ),
        )
        conn.commit()


def upsert_from_telegram_user(user: Any, *, source: str, event_type: str | None = None) -> None:
    if user is None:
        return
    upsert_telegram_user(
        getattr(user, "id", None),
        chat_id=getattr(user, "id", None),
        username=getattr(user, "username", None),
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        language_code=getattr(user, "language_code", None),
        is_bot=getattr(user, "is_bot", False),
        source=source,
        event_type=event_type,
    )


def summary() -> dict[str, int]:
    init_db()
    with connect_storage() as conn:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN can_broadcast THEN 1 ELSE 0 END) AS broadcastable,
              SUM(CASE WHEN NOT can_broadcast THEN 1 ELSE 0 END) AS disabled,
              SUM(CASE WHEN blocked_at IS NOT NULL THEN 1 ELSE 0 END) AS blocked
            FROM telegram_users
            """
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "broadcastable": int(row["broadcastable"] or 0),
        "disabled": int(row["disabled"] or 0),
        "blocked": int(row["blocked"] or 0),
    }


def list_users(*, limit: int = 150, offset: int = 0, include_disabled: bool = True) -> dict[str, Any]:
    init_db()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    where = "" if include_disabled else "WHERE can_broadcast = TRUE" if use_postgres() else "WHERE can_broadcast = 1"
    with connect_storage() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM telegram_users {where}").fetchone()["n"]
        rows = conn.execute(
            qp(
                f"""
                SELECT telegram_user_id, chat_id, username, first_name, last_name, language_code,
                       source, first_seen_at, last_seen_at, last_interaction_event, can_broadcast,
                       blocked_at, last_broadcast_at, broadcast_success_count,
                       broadcast_failure_count, last_broadcast_error
                FROM telegram_users
                {where}
                ORDER BY last_seen_at DESC
                LIMIT ? OFFSET ?
                """
            ),
            (limit, offset),
        ).fetchall()
    return {"total": total, "items": [_row_to_dict(row) for row in rows]}


def broadcast_recipients(*, limit: int = 500) -> list[dict[str, Any]]:
    init_db()
    limit = max(1, min(limit, 2000))
    with connect_storage() as conn:
        rows = conn.execute(
            qp(
                """
                SELECT telegram_user_id, chat_id, username, first_name
                FROM telegram_users
                WHERE can_broadcast = TRUE
                  AND blocked_at IS NULL
                ORDER BY last_seen_at DESC
                LIMIT ?
                """
            ),
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_broadcast_result(telegram_user_id: int, *, ok: bool, error: str | None = None) -> None:
    init_db()
    now = utc_now_iso()
    err = _clean_text(error, 1000)
    block = _should_disable_broadcast(err)
    if ok:
        sql = qp(
            """
            UPDATE telegram_users
            SET last_broadcast_at = ?,
                broadcast_success_count = broadcast_success_count + 1,
                last_broadcast_error = NULL
            WHERE telegram_user_id = ?
            """
        )
        params = (now, telegram_user_id)
    else:
        sql = qp(
            """
            UPDATE telegram_users
            SET last_broadcast_at = ?,
                broadcast_failure_count = broadcast_failure_count + 1,
                last_broadcast_error = ?,
                can_broadcast = CASE WHEN ? THEN FALSE ELSE can_broadcast END,
                blocked_at = CASE WHEN ? THEN COALESCE(blocked_at, ?) ELSE blocked_at END
            WHERE telegram_user_id = ?
            """
        )
        params = (now, err, block, block, now, telegram_user_id)
    with _lock, connect_storage() as conn:
        conn.execute(sql, params)
        conn.commit()


def send_broadcast(text: str, *, limit: int = 500, dry_run: bool = False) -> dict[str, Any]:
    recipients = broadcast_recipients(limit=limit)
    if dry_run:
        return {"ok": True, "dry_run": True, "target_count": len(recipients), "sent": 0, "failed": 0, "items": []}
    sent = 0
    failed = 0
    items: list[dict[str, Any]] = []
    for row in recipients:
        ok, err = send_telegram_message(row["chat_id"], text)
        mark_broadcast_result(int(row["telegram_user_id"]), ok=ok, error=err)
        if ok:
            sent += 1
        else:
            failed += 1
        items.append(
            {
                "telegram_user_id": row["telegram_user_id"],
                "username": row.get("username"),
                "first_name": row.get("first_name"),
                "ok": ok,
                "error": err,
            }
        )
    return {
        "ok": failed == 0,
        "dry_run": False,
        "target_count": len(recipients),
        "sent": sent,
        "failed": failed,
        "items": items,
    }


def _should_disable_broadcast(error: str | None) -> bool:
    err = (error or "").lower()
    return any(
        needle in err
        for needle in (
            "bot was blocked",
            "user is deactivated",
            "chat not found",
            "forbidden",
            "blocked by the user",
        )
    )


def _row_to_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["can_broadcast"] = bool(out.get("can_broadcast"))
    return out

"""Persistent activity/event storage for admin analytics."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from app.storage_connection import connect_storage, qp, use_postgres

_lock = threading.Lock()
_events_schema_initialized = False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    global _events_schema_initialized
    if _events_schema_initialized:
        return
    with _lock, connect_storage() as conn:
        if use_postgres():
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id BIGSERIAL PRIMARY KEY,
                    ts TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    telegram_user_id BIGINT,
                    username TEXT,
                    first_name TEXT,
                    meta_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
        else:
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
        conn.commit()
    _events_schema_initialized = True


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
    sql = qp(
        """
        INSERT INTO events (ts, event_type, source, telegram_user_id, username, first_name, meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """
    )
    with _lock, connect_storage() as conn:
        conn.execute(
            sql,
            (utc_now_iso(), event_type, source, telegram_user_id, username, first_name, payload),
        )
        conn.commit()


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

    with connect_storage() as conn:
        cnt_sql = qp(f"SELECT COUNT(*) AS n FROM events {where_sql}")
        total = conn.execute(cnt_sql, params).fetchone()["n"]
        q_sql = qp(
            f"""
            SELECT id, ts, event_type, source, telegram_user_id, username, first_name, meta_json
            FROM events
            {where_sql}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """
        )
        rows = conn.execute(q_sql, [*params, limit, offset]).fetchall()

    return {
        "total": total,
        "items": [_event_row_to_dict(row) for row in rows],
    }


def _day_expr() -> str:
    return "LEFT(ts::text, 10)" if use_postgres() else "substr(ts, 1, 10)"


def summary() -> dict[str, Any]:
    init_db()
    day_expr = _day_expr()
    with connect_storage() as conn:
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
                f"""
                SELECT {day_expr} AS day, COUNT(*) AS count
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
    """Aggregated per Telegram user for admin directory.

    Merges two sources:
    1. events table (users that sent Telegram initData with any request)
    2. payment_codes table redemption JSON (users identified at code-redemption time,
       even if initData was missing so they never appear in the events table directly)
    """
    init_db()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    with connect_storage() as conn:
        ev_rows = conn.execute(
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
            """
        ).fetchall()

        code_rows: list[Any] = []
        try:
            code_rows = conn.execute("SELECT entry_json FROM payment_codes").fetchall()
        except Exception:  # noqa: BLE001 — table may not exist yet
            pass

    merged: dict[int, dict[str, Any]] = {}
    for row in ev_rows:
        uid = row["telegram_user_id"]
        merged[uid] = {
            "telegram_user_id": uid,
            "username": row["username"],
            "first_name": row["first_name"],
            "event_count": row["event_count"],
            "last_seen_ts": row["last_seen_ts"],
            "redeem_count": int(row["redeem_count"] or 0),
            "pdf_generated_count": int(row["pdf_generated_count"] or 0),
            "pdf_download_count": int(row["pdf_download_count"] or 0),
            "bot_events_count": int(row["bot_events_count"] or 0),
        }

    for code_row in code_rows:
        try:
            entry = json.loads(code_row["entry_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict) or not entry.get("used"):
            continue
        red = entry.get("redemption")
        if not isinstance(red, dict):
            continue
        uid_raw = red.get("telegram_user_id")
        if uid_raw is None:
            continue
        try:
            uid = int(uid_raw)
        except (ValueError, TypeError):
            continue
        if uid in merged:
            if merged[uid]["redeem_count"] == 0:
                merged[uid]["redeem_count"] = 1
            continue
        redeemed_at = entry.get("redeemed_at") or entry.get("created_at") or ""
        existing = merged.get(uid)
        if existing is None:
            merged[uid] = {
                "telegram_user_id": uid,
                "username": red.get("username"),
                "first_name": red.get("first_name"),
                "event_count": 0,
                "last_seen_ts": redeemed_at,
                "redeem_count": 1,
                "pdf_generated_count": 0,
                "pdf_download_count": 0,
                "bot_events_count": 0,
                "from_code_only": True,
            }

    items = sorted(merged.values(), key=lambda r: r.get("last_seen_ts") or "", reverse=True)
    total = len(items)
    return {"total": total, "items": items[offset : offset + limit]}


def _event_row_to_dict(row: Any) -> dict[str, Any]:
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

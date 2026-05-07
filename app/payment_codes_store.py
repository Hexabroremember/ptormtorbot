"""One-time payment approval codes — SQLite-backed (same DB as analytics)."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR") or str(ROOT_DIR / "data"))
DB_PATH = DATA_DIR / "events.sqlite3"

# Legacy JSON path — migrated once into SQLite then renamed to .bak
LEGACY_JSON_PATH = DATA_DIR / "payment_codes.json"

_lock = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_codes (
            code TEXT PRIMARY KEY NOT NULL,
            entry_json TEXT NOT NULL
        )
        """
    )


def _migrate_legacy_json(conn: sqlite3.Connection) -> None:
    if not LEGACY_JSON_PATH.is_file():
        return
    n = conn.execute("SELECT COUNT(*) AS c FROM payment_codes").fetchone()["c"]
    if int(n) > 0:
        return
    try:
        raw = json.loads(LEGACY_JSON_PATH.read_text(encoding="utf-8"))
        codes = raw.get("codes") if isinstance(raw, dict) else None
        if not isinstance(codes, dict):
            return
        for code, entry in codes.items():
            if not isinstance(code, str) or not isinstance(entry, dict):
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO payment_codes (code, entry_json)
                VALUES (?, ?)
                """,
                (code, json.dumps(entry, ensure_ascii=False)),
            )
        conn.commit()
        bak = LEGACY_JSON_PATH.with_suffix(".json.bak")
        LEGACY_JSON_PATH.replace(bak)
    except (json.JSONDecodeError, OSError, sqlite3.Error):
        conn.rollback()


def normalize_code(raw: str) -> str:
    return "".join(raw.split()).upper()


def issue_new_code(*, meta: dict[str, Any] | None = None) -> str:
    """Generate and persist a new unused code; retries on collision."""
    with _lock:
        conn = _connect()
        try:
            _ensure_table(conn)
            _migrate_legacy_json(conn)
            for _ in range(50):
                code = secrets.token_hex(5).upper()
                exists = conn.execute(
                    "SELECT 1 FROM payment_codes WHERE code = ?", (code,)
                ).fetchone()
                if exists:
                    continue
                entry: dict[str, Any] = {"used": False, "created_at": _utc_now_iso()}
                if meta:
                    entry.update(meta)
                conn.execute(
                    """
                    INSERT INTO payment_codes (code, entry_json)
                    VALUES (?, ?)
                    """,
                    (code, json.dumps(entry, ensure_ascii=False)),
                )
                conn.commit()
                return code
        finally:
            conn.close()
    raise RuntimeError("Could not allocate unique payment code")


def list_codes(*, include_code: bool = False) -> list[dict[str, Any]]:
    """Return codes for admin views, newest first."""
    with _lock:
        conn = _connect()
        try:
            _ensure_table(conn)
            _migrate_legacy_json(conn)
            rows = conn.execute(
                "SELECT code, entry_json FROM payment_codes"
            ).fetchall()
        finally:
            conn.close()

    items: list[dict[str, Any]] = []
    for row in rows:
        code = row["code"]
        try:
            entry = json.loads(row["entry_json"])
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        code_value = code if include_code else f"{code[:3]}…{code[-3:]}"
        row_out: dict[str, Any] = {
            "code": code_value,
            "used": bool(entry.get("used")),
            "created_at": entry.get("created_at"),
            "redeemed_at": entry.get("redeemed_at"),
            "source": entry.get("source"),
            "order_id": entry.get("order_id"),
            "telegram_pdf_sent": bool(entry.get("telegram_pdf_sent")),
            "telegram_pdf_sent_at": entry.get("telegram_pdf_sent_at"),
        }
        red = entry.get("redemption")
        if isinstance(red, dict) and red:
            row_out["redemption"] = red
        items.append(row_out)
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items


def codes_summary() -> dict[str, int]:
    with _lock:
        conn = _connect()
        try:
            _ensure_table(conn)
            _migrate_legacy_json(conn)
            rows = conn.execute(
                "SELECT entry_json FROM payment_codes"
            ).fetchall()
        finally:
            conn.close()
    total = len(rows)
    used = 0
    for row in rows:
        try:
            entry = json.loads(row["entry_json"])
            if isinstance(entry, dict) and entry.get("used"):
                used += 1
        except json.JSONDecodeError:
            continue
    return {"total": total, "used": used, "unused": total - used}


def redeem_code(raw: str, *, redemption: dict[str, Any] | None = None) -> tuple[bool, str]:
    """
    Consume one code. Returns (success, message_key).
    message_key: 'ok' | 'not_found' | 'already_used'

    ``redemption`` is persisted on the code record for admin (form snapshot + Telegram id).
    """
    code = normalize_code(raw)
    if len(code) < 8:
        return False, "not_found"

    with _lock:
        conn = _connect()
        try:
            _ensure_table(conn)
            _migrate_legacy_json(conn)
            row = conn.execute(
                "SELECT entry_json FROM payment_codes WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                return False, "not_found"
            try:
                entry = json.loads(row["entry_json"])
            except json.JSONDecodeError:
                return False, "not_found"
            if not isinstance(entry, dict):
                return False, "not_found"
            if entry.get("used"):
                return False, "already_used"
            entry["used"] = True
            entry["redeemed_at"] = _utc_now_iso()
            if redemption:
                entry["redemption"] = redemption
            conn.execute(
                """
                UPDATE payment_codes SET entry_json = ?
                WHERE code = ?
                """,
                (json.dumps(entry, ensure_ascii=False), code),
            )
            conn.commit()
            return True, "ok"
        finally:
            conn.close()


def get_code_entry(raw: str) -> dict[str, Any] | None:
    code = normalize_code(raw)
    with _lock:
        conn = _connect()
        try:
            _ensure_table(conn)
            _migrate_legacy_json(conn)
            row = conn.execute(
                "SELECT entry_json FROM payment_codes WHERE code = ?", (code,)
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    try:
        entry = json.loads(row["entry_json"])
        return dict(entry) if isinstance(entry, dict) else None
    except json.JSONDecodeError:
        return None


def mark_code_telegram_pdf_sent(raw: str) -> None:
    code = normalize_code(raw)
    with _lock:
        conn = _connect()
        try:
            _ensure_table(conn)
            _migrate_legacy_json(conn)
            row = conn.execute(
                "SELECT entry_json FROM payment_codes WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                return
            try:
                entry = json.loads(row["entry_json"])
            except json.JSONDecodeError:
                return
            if not isinstance(entry, dict):
                return
            entry["telegram_pdf_sent"] = True
            entry["telegram_pdf_sent_at"] = _utc_now_iso()
            conn.execute(
                "UPDATE payment_codes SET entry_json = ? WHERE code = ?",
                (json.dumps(entry, ensure_ascii=False), code),
            )
            conn.commit()
        finally:
            conn.close()

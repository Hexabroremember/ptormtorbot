"""Persistent control flags for the admin panel — SQLite (same DB as analytics)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR") or str(ROOT_DIR / "data"))
DB_PATH = DATA_DIR / "events.sqlite3"

LEGACY_CONTROL_JSON = DATA_DIR / "admin_control.json"

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_kv_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_kv (
            key TEXT PRIMARY KEY NOT NULL,
            value TEXT NOT NULL
        )
        """
    )


def _migrate_legacy_json(conn: sqlite3.Connection) -> None:
    if not LEGACY_CONTROL_JSON.is_file():
        return
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM app_kv WHERE key = ?", ("maintenance_mode",)
    ).fetchone()
    if row and int(row["c"]) > 0:
        return
    try:
        data = json.loads(LEGACY_CONTROL_JSON.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        mm = "1" if bool(data.get("maintenance_mode")) else "0"
        conn.execute(
            """
            INSERT OR REPLACE INTO app_kv (key, value)
            VALUES ('maintenance_mode', ?)
            """,
            (mm,),
        )
        conn.commit()
        LEGACY_CONTROL_JSON.replace(LEGACY_CONTROL_JSON.with_suffix(".json.bak"))
    except (json.JSONDecodeError, OSError, sqlite3.Error):
        conn.rollback()


def get_control_state() -> dict[str, Any]:
    with _lock:
        conn = _connect()
        try:
            _ensure_kv_table(conn)
            _migrate_legacy_json(conn)
            row = conn.execute(
                "SELECT value FROM app_kv WHERE key = ?", ("maintenance_mode",)
            ).fetchone()
        finally:
            conn.close()
    mm = row and row["value"] == "1"
    return {"maintenance_mode": mm}


def set_maintenance_mode(enabled: bool) -> dict[str, Any]:
    val = "1" if enabled else "0"
    with _lock:
        conn = _connect()
        try:
            _ensure_kv_table(conn)
            _migrate_legacy_json(conn)
            conn.execute(
                """
                INSERT INTO app_kv (key, value)
                VALUES ('maintenance_mode', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (val,),
            )
            conn.commit()
        finally:
            conn.close()
    return get_control_state()


def maintenance_mode_enabled() -> bool:
    return bool(get_control_state().get("maintenance_mode"))

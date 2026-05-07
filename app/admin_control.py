"""Persistent control flags for the admin panel — SQLite or PostgreSQL (same DB as analytics)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from app.storage_connection import connect_storage, qp, use_postgres

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR") or str(ROOT_DIR / "data"))

LEGACY_CONTROL_JSON = DATA_DIR / "admin_control.json"

_lock = threading.Lock()


def _ensure_kv_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_kv (
            key TEXT PRIMARY KEY NOT NULL,
            value TEXT NOT NULL
        )
        """
    )


def _migrate_legacy_json(conn: Any) -> None:
    if not LEGACY_CONTROL_JSON.is_file():
        return
    row = conn.execute(
        qp("SELECT COUNT(*) AS c FROM app_kv WHERE key = ?"),
        ("maintenance_mode",),
    ).fetchone()
    if row and int(row["c"]) > 0:
        return
    try:
        data = json.loads(LEGACY_CONTROL_JSON.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        mm = "1" if bool(data.get("maintenance_mode")) else "0"
        if use_postgres():
            conn.execute(
                """
                INSERT INTO app_kv (key, value)
                VALUES ('maintenance_mode', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (mm,),
            )
        else:
            conn.execute(
                """
                INSERT OR REPLACE INTO app_kv (key, value)
                VALUES ('maintenance_mode', ?)
                """,
                (mm,),
            )
        conn.commit()
        LEGACY_CONTROL_JSON.replace(LEGACY_CONTROL_JSON.with_suffix(".json.bak"))
    except (json.JSONDecodeError, OSError):
        conn.rollback()


def get_control_state() -> dict[str, Any]:
    row = None
    with _lock:
        conn = connect_storage()
        try:
            _ensure_kv_table(conn)
            _migrate_legacy_json(conn)
            row = conn.execute(
                qp("SELECT value FROM app_kv WHERE key = ?"),
                ("maintenance_mode",),
            ).fetchone()
        finally:
            conn.close()
    mm = row and row["value"] == "1"
    return {"maintenance_mode": mm}


def set_maintenance_mode(enabled: bool) -> dict[str, Any]:
    val = "1" if enabled else "0"
    with _lock:
        conn = connect_storage()
        try:
            _ensure_kv_table(conn)
            _migrate_legacy_json(conn)
            if use_postgres():
                conn.execute(
                    """
                    INSERT INTO app_kv (key, value)
                    VALUES ('maintenance_mode', %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    (val,),
                )
            else:
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

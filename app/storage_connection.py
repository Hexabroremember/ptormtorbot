"""SQLite (local disk) or PostgreSQL (e.g. Supabase) based on DATABASE_URL."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR") or str(ROOT_DIR / "data"))
SQLITE_PATH = DATA_DIR / "events.sqlite3"


def _normalized_db_url() -> str:
    raw = (os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DATABASE_URL") or "").strip()
    if raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql://", 1)
    return raw


def use_postgres() -> bool:
    """True when a Postgres URL is configured (read at call time so ``load_dotenv`` applies)."""
    return bool(_normalized_db_url())


def database_url() -> str | None:
    u = _normalized_db_url()
    return u or None


def connect_storage():
    """Return a DB connection. Use ``qp()`` for placeholder conversion."""
    url = _normalized_db_url()
    if url:
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(url, row_factory=dict_row)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error:
        pass
    return conn


def qp(sql: str) -> str:
    """SQLite ``?`` placeholders → PostgreSQL ``%s``."""
    return sql.replace("?", "%s") if use_postgres() else sql


__all__ = ["connect_storage", "database_url", "qp", "use_postgres"]

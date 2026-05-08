"""SQLite (local disk) or PostgreSQL (e.g. Supabase) based on DATABASE_URL."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import quote, unquote


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR") or str(ROOT_DIR / "data"))
SQLITE_PATH = DATA_DIR / "events.sqlite3"


def _encode_credentials_in_pg_uri(url: str) -> str:
    """Percent-encode user/password so ``@``, ``:``, etc. in passwords do not break the URI.

    PostgreSQL URIs use the *last* ``@`` before the path as the boundary between
    ``user:password`` and ``host:port``. Pasted Supabase passwords often contain
    ``@`` (e.g. email-like passwords); without encoding, the host is parsed wrong.
    """
    if not url.startswith("postgresql://"):
        return url
    rest = url[len("postgresql://") :]
    slash = rest.find("/")
    if slash >= 0:
        authority, tail = rest[:slash], rest[slash:]
    else:
        authority, tail = rest, ""
    if "@" not in authority:
        return url

    userinfo, hostport = authority.rsplit("@", 1)
    colon = userinfo.find(":")
    if colon == -1:
        user_enc = quote(unquote(userinfo), safe="")
        return f"postgresql://{user_enc}@{hostport}{tail}"

    user_raw, pwd_raw = userinfo[:colon], userinfo[colon + 1 :]
    user_enc = quote(unquote(user_raw), safe="")
    pwd_enc = quote(unquote(pwd_raw), safe="")
    return f"postgresql://{user_enc}:{pwd_enc}@{hostport}{tail}"


def _normalized_db_url() -> str:
    raw = (os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DATABASE_URL") or "").strip()
    if raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql://", 1)
    return _encode_credentials_in_pg_uri(raw)


def _validate_pg_conninfo(url: str) -> None:
    """Reject obviously broken URIs after normalization."""
    import psycopg.conninfo

    try:
        opts = psycopg.conninfo.conninfo_to_dict(url)
    except Exception as exc:
        raise ValueError(
            "DATABASE_URL is not a valid PostgreSQL connection string. "
            "Copy the URI from Supabase → Project Settings → Database, "
            "and ensure special characters in the password are percent-encoded "
            "(``@`` → ``%40``, ``:`` → ``%3A``, ``#`` → ``%23``)."
        ) from exc
    host = (opts.get("host") or "").strip()
    if "@" in host:
        raise ValueError(
            "DATABASE_URL still looks malformed after auto-encoding (hostname contains ``@``). "
            "Reset the database password in Supabase or paste the URI exactly from the dashboard."
        )


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

        _validate_pg_conninfo(url)
        try:
            return psycopg.connect(url, row_factory=dict_row)
        except psycopg.OperationalError as exc:
            err = str(exc)
            if "Name or service not known" in err and "@" in err:
                raise ValueError(
                    "PostgreSQL DNS error — hostname often broken when ``@`` appears inside the "
                    "password in DATABASE_URL. Encode ``@`` as ``%40`` in the password, or use a "
                    "password without ``@``. See Supabase → Database → Connection string."
                ) from exc
            raise
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

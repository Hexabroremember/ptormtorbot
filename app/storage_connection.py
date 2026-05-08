"""SQLite (local disk) or PostgreSQL (e.g. Supabase) based on DATABASE_URL."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote, unquote


logger = logging.getLogger(__name__)

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


def _conninfo_prefer_ipv4(url: str) -> str:
    """If *host* has an IPv4 (A) record, set *hostaddr* so libpq connects over IPv4.

    Railway and similar hosts often have no IPv6 egress; Supabase ``db.*.supabase.co`` can
    resolve to IPv6 first, causing "Network is unreachable". Forcing *hostaddr* to the
    IPv4 address works when an A record exists. If there is no A record, return *url* unchanged
    (use Supabase pooler URI in that case).
    """
    if os.environ.get("DATABASE_PREFER_IPV4", "1").strip().lower() in ("0", "false", "no", "off"):
        return url

    import socket

    import psycopg.conninfo as ci

    try:
        opts = dict(ci.conninfo_to_dict(url))
    except Exception:
        return url

    host = (opts.get("host") or "").strip()
    if not host or host.startswith("/"):
        return url

    port_str = opts.get("port")
    try:
        port = int(port_str) if port_str is not None else 5432
    except (TypeError, ValueError):
        port = 5432

    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return url

    if not infos:
        return url

    ipv4 = infos[0][4][0]
    opts["hostaddr"] = ipv4
    try:
        return ci.make_conninfo(**opts)
    except Exception:
        return url


def _warn_if_supabase_direct_host(url: str) -> None:
    """Log once: direct ``db.*.supabase.co`` is IPv6-first; Railway often needs the pooler URI."""
    import psycopg.conninfo as ci

    try:
        opts = ci.conninfo_to_dict(url)
    except Exception:
        return
    host = (opts.get("host") or "").strip()
    if not re.match(r"^db\.[a-z0-9]+\.supabase\.co$", host, re.I):
        return
    logger.warning(
        "DATABASE_URL points at Supabase direct host %s (IPv6-first). Many clouds (e.g. Railway) "
        "cannot reach it. In Supabase Dashboard → Connect (or Database → Connection string), copy "
        "the Session pooler or Transaction pooler URI — host must contain pooler.supabase.com. "
        "Docs: https://supabase.com/docs/guides/platform/ipv4-address",
        host,
    )


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


_pg_conninfo_cache: str | None = None
_pg_conninfo_cache_key: str | None = None


def connect_storage():
    """Return a DB connection. Use ``qp()`` for placeholder conversion."""
    url = _normalized_db_url()
    if url:
        import psycopg
        from psycopg.rows import dict_row

        _validate_pg_conninfo(url)
        _warn_if_supabase_direct_host(url)
        global _pg_conninfo_cache, _pg_conninfo_cache_key
        if _pg_conninfo_cache_key == url and _pg_conninfo_cache is not None:
            conninfo = _pg_conninfo_cache
        else:
            conninfo = _conninfo_prefer_ipv4(url)
            _pg_conninfo_cache = conninfo
            _pg_conninfo_cache_key = url
        try:
            return psycopg.connect(conninfo, row_factory=dict_row)
        except psycopg.OperationalError as exc:
            err = str(exc)
            if "Name or service not known" in err and "@" in err:
                raise ValueError(
                    "PostgreSQL DNS error — hostname often broken when ``@`` appears inside the "
                    "password in DATABASE_URL. Encode ``@`` as ``%40`` in the password, or use a "
                    "password without ``@``. See Supabase → Database → Connection string."
                ) from exc
            if "Network is unreachable" in err:
                raise ValueError(
                    "PostgreSQL connection failed (network unreachable). Your DATABASE_URL likely "
                    "still uses Supabase *direct* host db.<project>.supabase.co — that endpoint is "
                    "IPv6-first and unreachable from Railway. Remove it and paste the full URI from "
                    "Supabase Dashboard → Connect → Connection string: choose **Session pooler** "
                    "(port 5432) or **Transaction pooler** (port 6543); the host must be "
                    "*.pooler.supabase.com (IPv4). Keep sslmode=require if present. "
                    "Optional paid alternative: IPv4 add-on for direct connections. "
                    "https://supabase.com/docs/guides/platform/ipv4-address"
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

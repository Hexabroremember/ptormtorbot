"""Short-lived PDF bytes keyed by token — lets Mini App download via HTTPS GET (blob URLs fail in Telegram WebView)."""

from __future__ import annotations

import secrets
import threading
import time

TTL_SECONDS = 900  # 15 minutes

_lock = threading.Lock()
_store: dict[str, tuple[bytes, float]] = {}


def _purge_expired(now: float) -> None:
    dead = [k for k, (_, exp) in _store.items() if exp <= now]
    for k in dead:
        del _store[k]


def register_pdf_bytes(data: bytes) -> str:
    token = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _lock:
        _purge_expired(now)
        _store[token] = (data, now + TTL_SECONDS)
    return token


def get_pdf_bytes(token: str) -> bytes | None:
    now = time.monotonic()
    with _lock:
        _purge_expired(now)
        entry = _store.get(token)
        if entry is None:
            return None
        blob, exp = entry
        if exp <= now:
            del _store[token]
            return None
        return blob

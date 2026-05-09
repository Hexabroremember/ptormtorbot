"""Resolve the app's public HTTPS origin for Telegram URL-fetch and webhooks."""

from __future__ import annotations

import os
from urllib.parse import urlparse


def _https_origin_from_domain_or_url(raw: str) -> str:
    """Normalize host or full URL to ``https://host`` (no path, no trailing slash)."""
    s = raw.strip().rstrip("/")
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    parsed = urlparse(s)
    if not parsed.netloc:
        return ""
    return f"https://{parsed.netloc}"


def effective_public_base_url() -> str:
    """Public base URL with no trailing slash.

    Prefer explicit ``WEB_APP_URL``, then Railway, Render, Fly.
    """
    explicit = os.environ.get("WEB_APP_URL", "").strip().rstrip("/")
    if explicit:
        return explicit

    railway = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway:
        return _https_origin_from_domain_or_url(railway)

    render = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if render:
        return _https_origin_from_domain_or_url(render)

    fly = os.environ.get("FLY_APP_NAME", "").strip()
    if fly:
        return f"https://{fly}.fly.dev"

    return ""

"""Resolve the app's public HTTPS origin for Telegram URL-fetch and webhooks."""

from __future__ import annotations

import os


def effective_public_base_url() -> str:
    """Public base URL with no trailing slash.

    Prefer explicit ``WEB_APP_URL``, then common platform env vars (Railway, Render, Fly).
    """
    explicit = os.environ.get("WEB_APP_URL", "").strip().rstrip("/")
    if explicit:
        return explicit

    railway = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway:
        if railway.startswith("https://"):
            railway = railway[8:]
        elif railway.startswith("http://"):
            railway = railway[7:]
        host = railway.split("/")[0]
        return f"https://{host}"

    render = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if render:
        return render

    fly = os.environ.get("FLY_APP_NAME", "").strip()
    if fly:
        return f"https://{fly}.fly.dev"

    return ""

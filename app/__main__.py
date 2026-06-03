"""Production entrypoint: reads PORT from the environment (no shell expansion required)."""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from app.nowpayments import nowpayments_key_configured, related_payment_env_names
from app.public_url import effective_public_base_url
from app.storage_connection import use_postgres

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[1]


def _should_start_telegram_bot_subprocess() -> bool:
    """When false, only Uvicorn runs — use if another host/process already polls this bot token."""
    raw = os.environ.get("START_TELEGRAM_BOT_SUBPROCESS", "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    # Alternative to START_TELEGRAM_BOT_SUBPROCESS=0: skip in-process getUpdates when updates are
    # handled elsewhere (webhook on another service, second bot worker, etc.).
    mode = os.environ.get("TELEGRAM_BOT_UPDATES_MODE", "polling").strip().lower()
    if mode in ("webhook", "none", "external", "off"):
        return False
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # Do not let a local .env override platform-injected secrets (Railway, etc.).
    load_dotenv(ROOT_DIR / ".env", override=False)

    pub = effective_public_base_url()
    if pub:
        logger.info("Public app URL for Telegram/callbacks: %s", pub)
    else:
        logger.warning(
            "Public app URL not resolved — set WEB_APP_URL or rely on "
            "RAILWAY_PUBLIC_DOMAIN, RENDER_EXTERNAL_URL, or FLY_APP_NAME so Telegram can fetch PDF links."
        )

    if use_postgres():
        logger.info("Database backend: PostgreSQL (DATABASE_URL / Supabase).")
    else:
        logger.info("Database backend: SQLite under DATA_DIR.")

    if nowpayments_key_configured():
        logger.info("NOWPayments API key in environment: yes")
    else:
        hint = related_payment_env_names()
        logger.warning(
            "NOWPayments API key in environment: no. Related env var names (not values): %s",
            hint if hint else "(none — add NOWPAYMENTS_API_KEY to this Railway service Variables)",
        )

    port = int(os.environ.get("PORT", "8000"))

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token and _should_start_telegram_bot_subprocess():
        # PTB/run_polling uses asyncio signal handlers → must run on the main thread of a process,
        # not a background thread (raises ValueError: set_wakeup_fd only works in main thread).
        from app.telegram_bot import run_bot_process_entry

        proc = mp.Process(
            target=run_bot_process_entry,
            args=(token,),
            name="telegram-bot",
            daemon=True,
        )
        proc.start()
        logger.info(
            "[telegram:bot] subprocess started pid=%s mode=polling (getUpdates); "
            "set START_TELEGRAM_BOT_SUBPROCESS=0 if another host polls this token",
            proc.pid,
        )
    elif token:
        mode = os.environ.get("TELEGRAM_BOT_UPDATES_MODE", "polling").strip().lower()
        if mode in ("webhook", "none", "external", "off"):
            logger.info(
                "[telegram:bot] subprocess skipped TELEGRAM_BOT_UPDATES_MODE=%s "
                "(no in-process polling — same token must not poll elsewhere unless intentional)",
                mode,
            )
        elif os.environ.get("START_TELEGRAM_BOT_SUBPROCESS", "1").strip().lower() in (
            "0",
            "false",
            "no",
            "off",
        ):
            logger.info(
                "[telegram:bot] subprocess skipped START_TELEGRAM_BOT_SUBPROCESS disabled; "
                "FastAPI webhook fallback will receive Telegram updates on this service"
            )
        else:
            logger.info(
                "[telegram:bot] subprocess skipped START_TELEGRAM_BOT_SUBPROCESS disabled; "
                "API-only mode — avoid duplicate getUpdates pollers for same TELEGRAM_BOT_TOKEN"
            )

    uvicorn.run("app.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

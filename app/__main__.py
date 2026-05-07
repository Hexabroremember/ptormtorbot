"""Production entrypoint: reads PORT from the environment (no shell expansion required)."""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from app.nowpayments import nowpayments_key_configured, related_payment_env_names

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[1]


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # Do not let a local .env override platform-injected secrets (Railway, Render, etc.).
    load_dotenv(ROOT_DIR / ".env", override=False)

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
    if token:
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
        logger.info("Telegram bot subprocess started (pid=%s).", proc.pid)

    uvicorn.run("app.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

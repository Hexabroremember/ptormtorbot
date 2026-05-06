"""Production entrypoint: reads PORT from the environment (no shell expansion required)."""

from __future__ import annotations

import logging
import os
import threading

import uvicorn

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", "8000"))

    if os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        from app.telegram_bot import run_bot_daemon

        threading.Thread(
            target=run_bot_daemon,
            name="telegram-bot",
            daemon=True,
        ).start()
        logger.info("Telegram bot thread started.")

    uvicorn.run("app.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

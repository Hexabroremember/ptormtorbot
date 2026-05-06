"""Production entrypoint: reads PORT from the environment (no shell expansion required)."""

from __future__ import annotations

import logging
import multiprocessing as mp
import os

import uvicorn

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
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

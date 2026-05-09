#!/usr/bin/env python3
"""Backfill ``events`` rows for ``payment_code_redeemed`` from used ``payment_codes`` entries.

Run from repo root::

    python scripts/backfill_payment_redeemed_events.py --dry-run
    python scripts/backfill_payment_redeemed_events.py --apply

Requires ``DATABASE_URL`` / SQLite same as the running app.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.activity_store import init_db, log_event  # noqa: E402
from app.payment_codes_store import normalize_code  # noqa: E402
from app.storage_connection import connect_storage, use_postgres  # noqa: E402


def backfill_key(norm_code: str, redeemed_at: str) -> str:
    return hashlib.sha256(f"{norm_code}|{redeemed_at or ''}".encode()).hexdigest()[:48]


def event_exists(conn, bf_key: str) -> bool:
    if use_postgres():
        row = conn.execute(
            """
            SELECT 1 AS x FROM events
            WHERE event_type = 'payment_code_redeemed'
              AND meta_json::jsonb->>'backfill_key' = %s
            LIMIT 1
            """,
            (bf_key,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT 1 AS x FROM events
            WHERE event_type = 'payment_code_redeemed'
              AND json_extract(meta_json, '$.backfill_key') = ?
            LIMIT 1
            """,
            (bf_key,),
        ).fetchone()
    return row is not None


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill payment_code_redeemed events")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    parser.add_argument("--apply", action="store_true", help="Insert missing events")
    args = parser.parse_args()
    if not args.dry_run and not args.apply:
        parser.error("Specify --dry-run or --apply")

    init_db()

    with connect_storage() as conn:
        rows = conn.execute("SELECT code, entry_json FROM payment_codes").fetchall()

    would_insert = 0
    skipped = 0

    for row in rows:
        code_raw = row["code"]
        try:
            entry = json.loads(row["entry_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict) or not entry.get("used"):
            continue
        red = entry.get("redemption")
        if not isinstance(red, dict):
            skipped += 1
            continue
        uid_raw = red.get("telegram_user_id")
        if uid_raw is None:
            skipped += 1
            continue
        try:
            uid = int(uid_raw)
        except (TypeError, ValueError):
            skipped += 1
            continue

        norm = normalize_code(code_raw)
        if len(norm) < 8:
            skipped += 1
            continue

        redeemed_at = str(entry.get("redeemed_at") or "")
        bf_key = backfill_key(norm, redeemed_at)

        with connect_storage() as conn:
            if event_exists(conn, bf_key):
                skipped += 1
                continue

        code_hint = norm[-4:].upper() if len(norm) >= 4 else None
        meta = {
            "code_last4": code_hint,
            "redemption": red,
            "telegram_pdf_sent": bool(entry.get("telegram_pdf_sent")),
            "telegram_user_resolved": True,
            "client": "backfill_script",
            "backfill": True,
            "backfill_key": bf_key,
        }

        would_insert += 1
        if args.dry_run:
            print(f"would insert  tg={uid}  code_hint={code_hint}  bf_key={bf_key[:16]}…")
            continue

        log_event(
            "payment_code_redeemed",
            source="backfill_script",
            telegram_user_id=uid,
            username=red.get("username") if isinstance(red.get("username"), str) else None,
            first_name=red.get("first_name") if isinstance(red.get("first_name"), str) else None,
            meta=meta,
        )

    print(
        f"Done: {'would insert' if args.dry_run else 'inserted'} {would_insert}, skipped_existing_or_invalid {skipped}"
    )


if __name__ == "__main__":
    main()

"""One-time payment approval codes — SQLite or PostgreSQL (same DB as analytics)."""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.storage_connection import connect_storage, qp, use_postgres

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR") or str(ROOT_DIR / "data"))

LEGACY_JSON_PATH = DATA_DIR / "payment_codes.json"

_lock = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_codes (
            code TEXT PRIMARY KEY NOT NULL,
            entry_json TEXT NOT NULL
        )
        """
    )


def _migrate_legacy_json(conn: Any) -> None:
    if not LEGACY_JSON_PATH.is_file():
        return
    n = conn.execute("SELECT COUNT(*) AS c FROM payment_codes").fetchone()["c"]
    if int(n) > 0:
        return
    try:
        raw = json.loads(LEGACY_JSON_PATH.read_text(encoding="utf-8"))
        codes = raw.get("codes") if isinstance(raw, dict) else None
        if not isinstance(codes, dict):
            return
        for code, entry in codes.items():
            if not isinstance(code, str) or not isinstance(entry, dict):
                continue
            if use_postgres():
                conn.execute(
                    qp(
                        """
                        INSERT INTO payment_codes (code, entry_json)
                        VALUES (?, ?)
                        ON CONFLICT (code) DO UPDATE SET entry_json = EXCLUDED.entry_json
                        """
                    ),
                    (code, json.dumps(entry, ensure_ascii=False)),
                )
            else:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO payment_codes (code, entry_json)
                    VALUES (?, ?)
                    """,
                    (code, json.dumps(entry, ensure_ascii=False)),
                )
        conn.commit()
        bak = LEGACY_JSON_PATH.with_suffix(".json.bak")
        LEGACY_JSON_PATH.replace(bak)
    except (json.JSONDecodeError, OSError):
        conn.rollback()


def normalize_code(raw: str) -> str:
    return "".join(raw.split()).upper()


def issue_new_code(*, meta: dict[str, Any] | None = None) -> str:
    """Generate and persist a new unused code; retries on collision."""
    with _lock:
        conn = connect_storage()
        try:
            _ensure_table(conn)
            _migrate_legacy_json(conn)
            for _ in range(50):
                code = secrets.token_hex(5).upper()
                exists = conn.execute(
                    qp("SELECT 1 FROM payment_codes WHERE code = ?"),
                    (code,),
                ).fetchone()
                if exists:
                    continue
                entry: dict[str, Any] = {"used": False, "created_at": _utc_now_iso()}
                if meta:
                    entry.update(meta)
                conn.execute(
                    qp(
                        """
                        INSERT INTO payment_codes (code, entry_json)
                        VALUES (?, ?)
                        """
                    ),
                    (code, json.dumps(entry, ensure_ascii=False)),
                )
                conn.commit()
                return code
        finally:
            conn.close()
    raise RuntimeError("Could not allocate unique payment code")


def list_codes(*, include_code: bool = False) -> list[dict[str, Any]]:
    """Return codes for admin views, newest first."""
    with _lock:
        conn = connect_storage()
        try:
            _ensure_table(conn)
            _migrate_legacy_json(conn)
            rows = conn.execute("SELECT code, entry_json FROM payment_codes").fetchall()
        finally:
            conn.close()

    items: list[dict[str, Any]] = []
    for row in rows:
        code = row["code"]
        try:
            entry = json.loads(row["entry_json"])
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        code_value = code if include_code else f"{code[:3]}…{code[-3:]}"
        row_out: dict[str, Any] = {
            "code": code_value,
            "used": bool(entry.get("used")),
            "created_at": entry.get("created_at"),
            "redeemed_at": entry.get("redeemed_at"),
            "source": entry.get("source"),
            "order_id": entry.get("order_id"),
            "telegram_pdf_sent": bool(entry.get("telegram_pdf_sent")),
            "telegram_pdf_sent_at": entry.get("telegram_pdf_sent_at"),
            "issue_scope": entry.get("issue_scope"),
            "expiry_option": entry.get("expiry_option"),
            "price_ils": entry.get("price_ils"),
            "issue_label": entry.get("issue_label"),
        }
        red = entry.get("redemption")
        if isinstance(red, dict) and red:
            row_out["redemption"] = red
        items.append(row_out)
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items


def codes_summary() -> dict[str, int]:
    with _lock:
        conn = connect_storage()
        try:
            _ensure_table(conn)
            _migrate_legacy_json(conn)
            rows = conn.execute("SELECT entry_json FROM payment_codes").fetchall()
        finally:
            conn.close()
    total = len(rows)
    used = 0
    for row in rows:
        try:
            entry = json.loads(row["entry_json"])
            if isinstance(entry, dict) and entry.get("used"):
                used += 1
        except json.JSONDecodeError:
            continue
    return {"total": total, "used": used, "unused": total - used}


def _tier_expiry_matches(code_entry: dict[str, Any], form_expiry_option: str | None) -> bool:
    """Tier-scoped codes require the Mini App expiry package to match issuance."""
    if code_entry.get("issue_scope") != "tier":
        return True
    required = code_entry.get("expiry_option")
    if not required:
        return True
    if not form_expiry_option or not str(form_expiry_option).strip():
        return False
    return str(form_expiry_option).strip() == str(required).strip()


def redeem_code(
    raw: str,
    *,
    redemption: dict[str, Any] | None = None,
    form_expiry_option: str | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Consume a code. On success returns the updated entry dict (single DB round-trip).

    ``form_expiry_option`` should match the client's selected tier when ``issue_scope`` is ``tier``.
    On expiry mismatch the code is **not** consumed; returns ``expiry_mismatch`` and the entry snapshot.
    """
    code = normalize_code(raw)
    if len(code) < 8:
        return False, "not_found", None

    with _lock:
        conn = connect_storage()
        try:
            _ensure_table(conn)
            _migrate_legacy_json(conn)
            row = conn.execute(
                qp("SELECT entry_json FROM payment_codes WHERE code = ?"),
                (code,),
            ).fetchone()
            if row is None:
                return False, "not_found", None
            try:
                entry = json.loads(row["entry_json"])
            except json.JSONDecodeError:
                return False, "not_found", None
            if not isinstance(entry, dict):
                return False, "not_found", None
            if not _tier_expiry_matches(entry, form_expiry_option):
                return False, "expiry_mismatch", dict(entry)
            if entry.get("used"):
                return False, "already_used", None
            entry["used"] = True
            entry["redeemed_at"] = _utc_now_iso()
            if redemption:
                entry["redemption"] = redemption
            conn.execute(
                qp(
                    """
                    UPDATE payment_codes SET entry_json = ?
                    WHERE code = ?
                    """
                ),
                (json.dumps(entry, ensure_ascii=False), code),
            )
            conn.commit()
            return True, "ok", dict(entry)
        finally:
            conn.close()


def get_code_entry(raw: str) -> dict[str, Any] | None:
    code = normalize_code(raw)
    with _lock:
        conn = connect_storage()
        try:
            _ensure_table(conn)
            _migrate_legacy_json(conn)
            row = conn.execute(
                qp("SELECT entry_json FROM payment_codes WHERE code = ?"),
                (code,),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    try:
        entry = json.loads(row["entry_json"])
        return dict(entry) if isinstance(entry, dict) else None
    except json.JSONDecodeError:
        return None


def mark_code_telegram_pdf_sent(raw: str) -> None:
    code = normalize_code(raw)
    with _lock:
        conn = connect_storage()
        try:
            _ensure_table(conn)
            _migrate_legacy_json(conn)
            row = conn.execute(
                qp("SELECT entry_json FROM payment_codes WHERE code = ?"),
                (code,),
            ).fetchone()
            if row is None:
                return
            try:
                entry = json.loads(row["entry_json"])
            except json.JSONDecodeError:
                return
            if not isinstance(entry, dict):
                return
            entry["telegram_pdf_sent"] = True
            entry["telegram_pdf_sent_at"] = _utc_now_iso()
            conn.execute(
                qp("UPDATE payment_codes SET entry_json = ? WHERE code = ?"),
                (json.dumps(entry, ensure_ascii=False), code),
            )
            conn.commit()
        finally:
            conn.close()

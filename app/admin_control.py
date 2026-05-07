"""Small persistent control flags managed by the admin panel."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR") or str(ROOT_DIR / "data"))
CONTROL_PATH = DATA_DIR / "admin_control.json"

_lock = threading.Lock()


def _load() -> dict[str, Any]:
    if not CONTROL_PATH.exists():
        return {"maintenance_mode": False}
    try:
        data = json.loads(CONTROL_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"maintenance_mode": False}
        data.setdefault("maintenance_mode", False)
        return data
    except (json.JSONDecodeError, OSError):
        return {"maintenance_mode": False}


def _write(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONTROL_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONTROL_PATH)


def get_control_state() -> dict[str, Any]:
    with _lock:
        return _load()


def set_maintenance_mode(enabled: bool) -> dict[str, Any]:
    with _lock:
        data = _load()
        data["maintenance_mode"] = bool(enabled)
        _write(data)
        return data


def maintenance_mode_enabled() -> bool:
    return bool(get_control_state().get("maintenance_mode"))

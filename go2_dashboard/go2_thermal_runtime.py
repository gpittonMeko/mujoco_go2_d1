"""Stato runtime termico persistente (sopravvive al restart del servizio :5054)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RUNTIME_FILE = PROJECT_ROOT / "motor_health_thermal_runtime.json"
_LOCK = threading.Lock()


def _load() -> dict[str, Any]:
    if not _RUNTIME_FILE.is_file():
        return {}
    try:
        data = json.loads(_RUNTIME_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict[str, Any]) -> None:
    try:
        _RUNTIME_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_runtime_state() -> dict[str, Any]:
    with _LOCK:
        return dict(_load())


def pending_crouch_recovery() -> bool:
    with _LOCK:
        return bool(_load().get("pending_crouch_recovery"))


def arm_crouch_recovery(*, trigger: str = "unknown", motors: list[dict[str, Any]] | None = None) -> None:
    """Segna che il cane è accucciato e va rialzato appena temp < soglia crouch."""
    with _LOCK:
        data = _load()
        data["pending_crouch_recovery"] = True
        data["crouch_armed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        data["crouch_trigger"] = str(trigger)
        if motors:
            data["crouch_motors"] = [
                {"name": str(m.get("name")), "temperature_c": int(m.get("temperature_c", 0))}
                for m in motors
            ]
        _save(data)


def clear_crouch_recovery(*, reason: str = "stand_up") -> None:
    with _LOCK:
        data = _load()
        data["pending_crouch_recovery"] = False
        data["crouch_cleared_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        data["crouch_clear_reason"] = str(reason)
        _save(data)

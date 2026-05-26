"""Ultimo JSON di ``POST /api/grasp/plan`` (proxy verso worker) per coerenza viewer 3D."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

_lock = threading.Lock()
_last_plan: dict[str, Any] | None = None
_last_mono: float = 0.0


def set_last_grasp_plan(plan: dict[str, Any]) -> None:
    global _last_plan, _last_mono
    if not isinstance(plan, dict):
        return
    with _lock:
        _last_plan = dict(plan)
        _last_mono = time.monotonic()


def get_last_grasp_plan() -> dict[str, Any] | None:
    ttl = float((os.environ.get("GO2_GRASP_PLAN_CACHE_TTL_S") or "180").strip() or "180")
    with _lock:
        if _last_plan is None:
            return None
        if ttl > 0 and (time.monotonic() - _last_mono) > ttl:
            return None
        return dict(_last_plan)

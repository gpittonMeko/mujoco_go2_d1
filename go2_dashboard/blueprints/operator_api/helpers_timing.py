"""Timing richieste HTTP (dashboard operator API)."""

from __future__ import annotations

import time
from typing import Any

from flask import g


def merge_http_timing_into_json_dict(payload: dict[str, Any]) -> dict[str, Any]:
    """Aggiunge ``_http_timing_ms.server_total_ms`` dal timestamp salvato in ``g`` (before_request blueprint)."""
    out = dict(payload)
    t0 = getattr(g, "_operator_api_t0", None)
    if t0 is None:
        return out
    out["_http_timing_ms"] = {"server_total_ms": round((time.perf_counter() - t0) * 1000.0, 2)}
    return out

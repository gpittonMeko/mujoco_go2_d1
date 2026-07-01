"""Offset online da errori TCP post-mossa (EMA) — auto-calib operativa senza teach manuale."""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT

_CALIB_PATH = PROJECT_ROOT / "data" / "grasp_online_calib.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_float(key: str, default: float) -> float:
    try:
        return float((os.environ.get(key) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def load_store() -> dict[str, Any]:
    try:
        data = json.loads(_CALIB_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("version", 1)
            data.setdefault("delta_tcp_m", [0.0, 0.0, 0.0])
            data.setdefault("sample_count", 0)
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "delta_tcp_m": [0.0, 0.0, 0.0], "sample_count": 0}


def save_store(store: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("GO2_LOCAL", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "go2_local_off"}
    _CALIB_PATH.parent.mkdir(parents=True, exist_ok=True)
    store["updated_at"] = _now_iso()
    _CALIB_PATH.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "path": str(_CALIB_PATH)}


def record_tcp_error(
    planned_wp: list[float],
    actual_tip: list[float],
) -> dict[str, Any]:
    """Aggiorna EMA del delta TCP (actual - planned)."""
    if len(planned_wp) < 3 or len(actual_tip) < 3:
        return {"ok": False, "reason": "bad_vectors"}
    err = [float(actual_tip[i]) - float(planned_wp[i]) for i in range(3)]
    alpha = _env_float("GO2_GRASP_ONLINE_CALIB_ALPHA", 0.25)
    alpha = max(0.05, min(alpha, 0.6))
    store = load_store()
    old = store.get("delta_tcp_m") or [0.0, 0.0, 0.0]
    new = [round((1.0 - alpha) * float(old[i]) + alpha * err[i], 5) for i in range(3)]
    store["delta_tcp_m"] = new
    store["sample_count"] = int(store.get("sample_count") or 0) + 1
    store["last_error_m"] = [round(e, 4) for e in err]
    store["last_error_norm_m"] = round(math.sqrt(sum(e * e for e in err)), 4)
    save_store(store)
    return {
        "ok": True,
        "delta_tcp_m": new,
        "last_error_m": store["last_error_m"],
        "sample_count": store["sample_count"],
    }


def apply_online_offset(plan: dict[str, Any]) -> dict[str, Any]:
    """Somma offset EMA al target metrico dinamico (in-place)."""
    if not plan.get("ok"):
        return plan
    store = load_store()
    min_samples = int(_env_float("GO2_GRASP_ONLINE_CALIB_MIN_SAMPLES", 2))
    if int(store.get("sample_count") or 0) < min_samples:
        plan["online_calib_applied"] = False
        return plan
    delta = store.get("delta_tcp_m")
    if not isinstance(delta, list) or len(delta) < 3:
        plan["online_calib_applied"] = False
        return plan
    tgt = plan.get("grasp_display_base_link_m")
    if not isinstance(tgt, list) or len(tgt) < 3:
        plan["online_calib_applied"] = False
        return plan
    corrected = [round(float(tgt[i]) + float(delta[i]), 4) for i in range(3)]
    plan["grasp_display_base_link_m"] = corrected
    plan["online_calib_applied"] = True
    plan["online_calib_delta_tcp_m"] = [round(float(delta[i]), 4) for i in range(3)]
    preview = plan.get("preview") if isinstance(plan.get("preview"), dict) else {}
    stages = preview.get("plan") if isinstance(preview.get("plan"), list) else []
    for st in stages:
        if not isinstance(st, dict):
            continue
        st_tgt = st.get("target_xyz_m")
        if isinstance(st_tgt, list) and len(st_tgt) >= 3:
            st["target_xyz_m"] = [
                round(float(st_tgt[i]) + float(delta[i]), 4) for i in range(3)
            ]
            st["online_calib_offset"] = True
    if isinstance(plan.get("target"), dict):
        plan["target"]["base_xyz_m"] = list(corrected)
    return plan

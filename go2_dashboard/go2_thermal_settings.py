"""Impostazioni runtime protezione termica e autobilanciamento peso."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SETTINGS_FILE = PROJECT_ROOT / "motor_health_thermal_settings.json"
_LOCK = threading.Lock()

_DEFAULTS: dict[str, Any] = {
    "weight_balance_enabled": True,
    "pre_balance_crouch": True,
    "balance_threshold_c": 48,
    "crouch_threshold_c": 62,
    "imbalance_front_high": 0.58,
    "imbalance_front_low": 0.42,
    "imbalance_side_high": 0.58,
    "imbalance_side_low": 0.42,
    "shift_vx_mps": 0.08,
    "shift_vy_mps": 0.06,
    "shift_duration_s": 0.65,
    "shift_settle_s": 0.5,
    "recovery_stand_enabled": True,
    "threshold_hysteresis_c": 7,
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def defaults_from_env() -> dict[str, Any]:
    return {
        "weight_balance_enabled": _env_bool("GO2_THERMAL_WEIGHT_BALANCE", _DEFAULTS["weight_balance_enabled"]),
        "pre_balance_crouch": _env_bool("GO2_CROUCH_PRE_BALANCE", _DEFAULTS["pre_balance_crouch"]),
        "balance_threshold_c": int(os.environ.get("GO2_THERMAL_BALANCE_C", str(_DEFAULTS["balance_threshold_c"]))),
        "crouch_threshold_c": int(os.environ.get("GO2_THERMAL_CROUCH_C", str(_DEFAULTS["crouch_threshold_c"]))),
        "imbalance_front_high": _env_float("GO2_THERMAL_IMBALANCE_FRONT_HIGH", _DEFAULTS["imbalance_front_high"]),
        "imbalance_front_low": _env_float("GO2_THERMAL_IMBALANCE_FRONT_LOW", _DEFAULTS["imbalance_front_low"]),
        "imbalance_side_high": _env_float("GO2_THERMAL_IMBALANCE_SIDE_HIGH", _DEFAULTS["imbalance_side_high"]),
        "imbalance_side_low": _env_float("GO2_THERMAL_IMBALANCE_SIDE_LOW", _DEFAULTS["imbalance_side_low"]),
        "shift_vx_mps": _env_float("GO2_THERMAL_WEIGHT_SHIFT_VX", _DEFAULTS["shift_vx_mps"]),
        "shift_vy_mps": _env_float("GO2_THERMAL_WEIGHT_SHIFT_VY", _DEFAULTS["shift_vy_mps"]),
        "shift_duration_s": _env_float("GO2_THERMAL_WEIGHT_SHIFT_DURATION_S", _DEFAULTS["shift_duration_s"]),
        "shift_settle_s": _env_float("GO2_THERMAL_WEIGHT_SHIFT_SETTLE_S", _DEFAULTS["shift_settle_s"]),
        "recovery_stand_enabled": _env_bool("GO2_THERMAL_RECOVERY_STAND", _DEFAULTS["recovery_stand_enabled"]),
        "threshold_hysteresis_c": int(os.environ.get("GO2_THERMAL_HYSTERESIS_C", str(_DEFAULTS["threshold_hysteresis_c"]))),
    }


def thermal_hysteresis_c() -> int:
    return max(0, int(get_thermal_settings().get("threshold_hysteresis_c", 7)))


def balance_clear_threshold_c() -> int:
    """Uscita autobilanciamento → stand up: soglia bilancio − isteresi."""
    cfg = get_thermal_settings()
    balance_c = int(cfg.get("balance_threshold_c", 48))
    return max(0, balance_c - thermal_hysteresis_c())


def crouch_recovery_threshold_c() -> int:
    """Uscita crouch → autobilanciamento: soglia crouch − isteresi (es. 62−7 = 55°C)."""
    cfg = get_thermal_settings()
    crouch_c = int(cfg.get("crouch_threshold_c", 62))
    h = thermal_hysteresis_c()
    return max(0, crouch_c - h)


def crouch_stay_min_threshold_c() -> int:
    """Temp max > soglia recupero → resta in crouch (es. >55°C, resta fino a 56°C)."""
    return crouch_recovery_threshold_c() + 1


def leg_max_temperature_c(legs: list[dict[str, Any]]) -> tuple[int, str | None]:
    if not legs:
        return 0, None
    best = max(legs, key=lambda m: int(m.get("temperature_c", 0)))
    return int(best.get("temperature_c", 0)), str(best.get("name")) if best.get("name") else None


def all_leg_motors_below(legs: list[dict[str, Any]], threshold_c: int) -> bool:
    if not legs:
        return False
    return all(int(m.get("temperature_c", 0)) < int(threshold_c) for m in legs)


def thermal_threshold_band() -> dict[str, int]:
    cfg = get_thermal_settings()
    balance_c = int(cfg.get("balance_threshold_c", 48))
    crouch_c = int(cfg.get("crouch_threshold_c", 62))
    h = thermal_hysteresis_c()
    return {
        "balance_threshold_c": balance_c,
        "crouch_threshold_c": crouch_c,
        "threshold_hysteresis_c": h,
        "balance_clear_threshold_c": max(0, balance_c - h),
        "crouch_recovery_threshold_c": max(0, crouch_c - h),
        "crouch_stay_min_threshold_c": max(0, crouch_c - h) + 1,
    }


def _load_file() -> dict[str, Any]:
    if not _SETTINGS_FILE.is_file():
        return {}
    try:
        data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_file(data: dict[str, Any]) -> None:
    try:
        _SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


_RUNTIME: dict[str, Any] = {**defaults_from_env(), **_load_file()}


def get_thermal_settings() -> dict[str, Any]:
    with _LOCK:
        return dict(_RUNTIME)


def update_thermal_settings(patch: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    allowed = set(_DEFAULTS.keys())
    with _LOCK:
        for key, val in patch.items():
            if key not in allowed:
                continue
            if isinstance(_DEFAULTS[key], bool):
                if isinstance(val, bool):
                    _RUNTIME[key] = val
                else:
                    _RUNTIME[key] = str(val).strip().lower() in {"1", "true", "yes", "on"}
            elif isinstance(_DEFAULTS[key], float):
                try:
                    _RUNTIME[key] = float(val)
                except (TypeError, ValueError):
                    continue
            elif isinstance(_DEFAULTS[key], int):
                try:
                    _RUNTIME[key] = int(val)
                except (TypeError, ValueError):
                    continue
            else:
                _RUNTIME[key] = val
        out = dict(_RUNTIME)
        if persist:
            _save_file(out)
    return out


def analyze_foot_load(foot_force: list[int]) -> dict[str, Any]:
    """Carico per zampa FR, FL, RR, RL + quote anteriori/posteriori e sinistra/destra."""
    if not foot_force or len(foot_force) < 4:
        return {
            "ok": False,
            "hint_it": "Forze zampa non disponibili.",
            "front_share": None,
            "left_share": None,
            "raw": [],
        }
    fr, fl, rr, rl = (max(0, int(x)) for x in foot_force[:4])
    front = fr + fl
    rear = rr + rl
    left = fl + rl
    right = fr + rr
    total_ap = front + rear
    total_lr = left + right
    front_share = round(front / total_ap, 3) if total_ap > 0 else None
    left_share = round(left / total_lr, 3) if total_lr > 0 else None

    hint = "Carico relativamente bilanciato."
    if front_share is not None:
        if front_share >= 0.58:
            hint = "Carico sulle zampe anteriori elevato — tipico con braccio/payload in avanti."
        elif front_share <= 0.42:
            hint = "Carico sulle posteriori elevato."
    if left_share is not None:
        if left_share >= 0.58:
            hint += " Più peso sul lato sinistro."
        elif left_share <= 0.42:
            hint += " Più peso sul lato destro."

    return {
        "ok": True,
        "hint_it": hint,
        "front_share": front_share,
        "left_share": left_share,
        "per_leg": {"FR": fr, "FL": fl, "RR": rr, "RL": rl},
        "raw": [fr, fl, rr, rl],
    }


def _hot_leg_bias(hot_motors: list[dict[str, Any]]) -> dict[str, int]:
    front = rear = left = right = 0
    for m in hot_motors:
        name = str(m.get("name") or "")
        if name.startswith("FR"):
            front += 1
            right += 1
        elif name.startswith("FL"):
            front += 1
            left += 1
        elif name.startswith("RR"):
            rear += 1
            right += 1
        elif name.startswith("RL"):
            rear += 1
            left += 1
    return {"front": front, "rear": rear, "left": left, "right": right}


def plan_weight_redistribution(
    foot_force: list[int],
    hot_motors: list[dict[str, Any]],
    settings: dict[str, Any] | None = None,
    *,
    force_hot_bias: bool = False,
) -> dict[str, Any] | None:
    """
    Piano micro-spostamento per scaricare lato più caldo / più caricato.
    Ritorna None se non serve spostamento.
    ``force_hot_bias``: se True e ci sono motori caldi ma carico zampa in banda,
    forza uno shift verso il lato opposto ai motori più caldi (bilanciamento manuale).
    """
    cfg = settings or get_thermal_settings()
    if not cfg.get("weight_balance_enabled"):
        return None

    load = analyze_foot_load(foot_force)
    vx = vy = 0.0
    reasons: list[str] = []

    f_hi = float(cfg["imbalance_front_high"])
    f_lo = float(cfg["imbalance_front_low"])
    s_hi = float(cfg["imbalance_side_high"])
    s_lo = float(cfg["imbalance_side_low"])
    max_vx = abs(float(cfg["shift_vx_mps"]))
    max_vy = abs(float(cfg["shift_vy_mps"]))

    share = load.get("front_share")
    if share is not None:
        if share >= f_hi:
            vx = -max_vx
            reasons.append(f"anteriori {int(share * 100)}%")
        elif share <= f_lo:
            vx = max_vx
            reasons.append(f"posteriori {int((1 - share) * 100)}%")

    lshare = load.get("left_share")
    if lshare is not None:
        if lshare >= s_hi:
            vy = -max_vy
            reasons.append(f"sinistra {int(lshare * 100)}%")
        elif lshare <= s_lo:
            vy = max_vy
            reasons.append(f"destra {int((1 - lshare) * 100)}%")

    bias = _hot_leg_bias(hot_motors)
    if bias["front"] > bias["rear"] and vx >= 0:
        vx = -max_vx
        reasons.append("motori anteriori caldi")
    elif bias["rear"] > bias["front"] and vx <= 0:
        vx = max_vx
        reasons.append("motori posteriori caldi")
    if bias["left"] > bias["right"] and vy >= 0:
        vy = -max_vy
        reasons.append("motori sinistra caldi")
    elif bias["right"] > bias["left"] and vy <= 0:
        vy = max_vy
        reasons.append("motori destra caldi")

    if force_hot_bias and hot_motors and abs(vx) < 1e-6 and abs(vy) < 1e-6:
        if bias["front"] > bias["rear"]:
            vx = -max_vx
            reasons.append("motori anteriori caldi (bilancio manuale)")
        elif bias["rear"] > bias["front"]:
            vx = max_vx
            reasons.append("motori posteriori caldi (bilancio manuale)")
        if bias["left"] > bias["right"]:
            vy = -max_vy
            reasons.append("motori sinistra caldi (bilancio manuale)")
        elif bias["right"] > bias["left"]:
            vy = max_vy
            reasons.append("motori destra caldi (bilancio manuale)")

    if abs(vx) < 1e-6 and abs(vy) < 1e-6:
        return None

    return {
        "vx": round(vx, 4),
        "vy": round(vy, 4),
        "vyaw": 0.0,
        "duration_s": float(cfg["shift_duration_s"]),
        "settle_s": float(cfg["shift_settle_s"]),
        "reasons": reasons,
        "load": load,
    }

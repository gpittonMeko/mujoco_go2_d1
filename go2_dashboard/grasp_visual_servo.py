"""Visual servo semplificato da detection bbox (norm) — centratura polso prima della presa metrica."""

from __future__ import annotations

import math
import os
from typing import Any


def _env_float(key: str, default: float) -> float:
    try:
        return float((os.environ.get(key) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _tolist(val: Any, default: list | None = None) -> list | None:
    """Converte sequenze (incl. numpy ndarray) senza usare ``bool(array)``."""
    if val is None:
        return default
    if isinstance(val, (list, tuple)):
        return list(val)
    try:
        import numpy as np

        if isinstance(val, np.ndarray):
            return val.tolist()
    except Exception:
        pass
    try:
        return list(val)
    except TypeError:
        return default


def center_hints_from_detection(det: dict[str, Any], frame_hw: tuple[float, float]) -> dict[str, Any]:
    """Offset grip point vs centro immagine. ``frame_hw``: (h, w)."""
    h, w = float(frame_hw[0]), float(frame_hw[1])
    cx, cy = w / 2.0, h / 2.0
    bc = _tolist(det.get("bbox_center_px")) or _tolist(det.get("grip_center_px"))
    if bc is None or len(bc) < 2:
        norm = det.get("norm")
        if isinstance(norm, (list, tuple)) and len(norm) >= 2:
            nx, ny = float(norm[0]), float(norm[1])
            dx, dy = nx * (w / 2.0), ny * (h / 2.0)
        else:
            return {
                "has_grip_point": False,
                "norm": (0.0, 0.0),
                "offset_px": (0.0, 0.0),
                "yaw_deg": 0.0,
                "wrist_trim_deg": 0.0,
                "shoulder_trim_deg": 0.0,
            }
    else:
        mean_x, mean_y = float(bc[0]), float(bc[1])
        dx, dy = mean_x - cx, mean_y - cy
        nx = dx / max(w / 2.0, 1.0)
        ny = dy / max(h / 2.0, 1.0)
    nx_s = _env_float("GO2_WRIST_CENTER_NX_EFFECT_SIGN", 1.0)
    ny_s = _env_float("GO2_WRIST_CENTER_NY_EFFECT_SIGN", 1.0)
    yaw_deg = max(-12.0, min(12.0, nx * 10.0 * nx_s))
    wrist_trim_deg = max(-12.0, min(12.0, -ny * 11.0 * ny_s))
    shoulder_trim_deg = max(-4.5, min(4.5, -ny * 3.5 * ny_s))
    return {
        "has_grip_point": True,
        "source": det.get("backend") or "bbox",
        "yaw_deg": yaw_deg,
        "wrist_trim_deg": wrist_trim_deg,
        "shoulder_trim_deg": shoulder_trim_deg,
        "offset_px": (round(dx, 2), round(dy, 2)),
        "norm": (round(nx, 4), round(ny, 4)),
        "box_area_px": det.get("bbox_area_px"),
    }


def visual_servo_metric(det: dict[str, Any], frame_hw: tuple[float, float]) -> dict[str, Any]:
    hints = center_hints_from_detection(det, frame_hw)
    norm = hints.get("norm") or (0.0, 0.0)
    err = float(math.hypot(float(norm[0]), float(norm[1])))
    area = hints.get("box_area_px")
    size = float(area or 0.0)
    return {
        "ok": bool(hints.get("has_grip_point")),
        "error_norm": round(err, 5),
        "size_score": round(size, 3),
        "source": hints.get("source"),
        "hints": hints,
    }


def visual_servo_progress(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    if not before.get("ok") or not after.get("ok"):
        return {"ok": False, "reason": "missing_visual_metric"}
    err_before = float(before.get("error_norm") or 0.0)
    err_after = float(after.get("error_norm") or 0.0)
    err_gain = err_before - err_after
    min_err = _env_float("GO2_VISUAL_SERVO_MIN_ERR_GAIN", 0.018)
    return {
        "ok": bool(err_gain >= min_err),
        "err_gain": round(err_gain, 5),
        "before": before,
        "after": after,
    }


def wrist_center_joint_deltas(
    det: dict[str, Any],
    frame_hw: tuple[float, float],
    current_deg: list[float],
) -> dict[str, Any] | None:
    """Micro-step giunti J0/J1/J4 per centrare bbox. None se dentro deadband."""
    hints = center_hints_from_detection(det, frame_hw)
    if not hints.get("has_grip_point"):
        return None
    deadband = _env_float("GO2_WRIST_CENTER_DEADBAND_PX", 18.0)
    dx, dy = hints["offset_px"]
    if math.hypot(dx, dy) < deadband:
        return None
    gain = _env_float("GO2_WRIST_CENTER_STEP_GAIN", 0.38)
    max_yaw = _env_float("GO2_WRIST_CENTER_MAX_YAW_STEP_DEG", 1.2)
    max_shoulder = _env_float("GO2_WRIST_CENTER_MAX_SHOULDER_STEP_DEG", 0.9)
    max_wrist = _env_float("GO2_WRIST_CENTER_MAX_WRIST_STEP_DEG", 1.6)
    yaw_delta = max(-max_yaw, min(max_yaw, gain * float(hints["yaw_deg"])))
    shoulder_delta = max(-max_shoulder, min(max_shoulder, gain * float(hints["shoulder_trim_deg"])))
    wrist_delta = max(-max_wrist, min(max_wrist, gain * float(hints["wrist_trim_deg"])))
    cd = [float(current_deg[i]) for i in range(min(7, len(current_deg)))]
    while len(cd) < 7:
        cd.append(0.0)
    return {
        "joint_deltas_deg": {
            0: round(yaw_delta, 3),
            1: round(shoulder_delta, 3),
            4: round(wrist_delta, 3),
        },
        "hints": hints,
    }


def wrist_extend_toward_object_deltas(
    det: dict[str, Any],
    frame_hw: tuple[float, float],
    current_deg: list[float],
    *,
    rgb_fallback: bool = False,
) -> dict[str, Any] | None:
    """Estensione verso l'oggetto (J1/J2) quando bbox centrato ma ancora piccolo = lontano."""
    hints = center_hints_from_detection(det, frame_hw)
    if not hints.get("has_grip_point"):
        return None
    dx, dy = hints["offset_px"]
    center_tol = _env_float("GO2_WRIST_APPROACH_CENTER_TOL_PX", 28.0)
    if math.hypot(dx, dy) > center_tol:
        return None
    try:
        ar = float(det.get("bbox_area_ratio") or 0.0)
    except (TypeError, ValueError):
        ar = 0.0
    if ar <= 0:
        try:
            ar = float(det.get("bbox_area_px") or 0.0) / max(float(frame_hw[0]) * float(frame_hw[1]), 1.0)
        except (TypeError, ValueError):
            ar = 0.02
    target_ar = _env_float("GO2_WRIST_APPROACH_TARGET_AREA_RATIO", 0.085)
    if ar >= target_ar:
        return None
    gain = _env_float("GO2_WRIST_APPROACH_AREA_GAIN", 1.35 if rgb_fallback else 1.0)
    shoulder = _env_float("GO2_WRIST_APPROACH_SHOULDER_DEG", 3.2 if rgb_fallback else 1.8)
    elbow = _env_float("GO2_WRIST_APPROACH_ELBOW_DEG", 2.4 if rgb_fallback else 1.4)
    scale = max(0.35, min(1.5, gain * math.sqrt(target_ar / max(ar, 0.004))))
    shoulder_d = max(0.4, min(shoulder, shoulder * scale))
    elbow_d = max(0.3, min(elbow, elbow * scale))
    cd = [float(current_deg[i]) for i in range(min(7, len(current_deg)))]
    while len(cd) < 7:
        cd.append(0.0)
    return {
        "joint_deltas_deg": {
            1: round(shoulder_d, 3),
            2: round(elbow_d, 3),
        },
        "approach_scale": round(scale, 3),
        "bbox_area_ratio": round(ar, 5),
        "rgb_fallback": rgb_fallback,
    }


def apply_joint_deltas(
    servo: list[float],
    deltas: dict[int, float] | None,
    publish_move_one_joint_deg: Any,
) -> bool:
    if not deltas:
        return False
    for jidx, delta in deltas.items():
        publish_move_one_joint_deg(int(jidx), float(servo[int(jidx)]) + float(delta))
    return True

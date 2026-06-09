"""ROI visione: esclude zona pinza (tipico bordo inferiore frame polso)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT

_CROP_PATH = Path(
    os.environ.get(
        "D1_PICK_VISION_CROP_PATH",
        str(PROJECT_ROOT / "data" / "d1_pick_vision_crop.json"),
    )
)


def _frac(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0.0, min(0.45, float(raw)))
    except ValueError:
        return default


def _clamp_frac(v: float) -> float:
    return max(0.0, min(0.45, float(v)))


def _env_default_fracs() -> dict[str, float]:
    return {
        "top": _frac("D1_PICK_VISION_CROP_TOP_FRAC", 0.0),
        "bottom": _frac("D1_PICK_VISION_CROP_BOTTOM_FRAC", 0.30),
        "left": _frac("D1_PICK_VISION_CROP_LEFT_FRAC", 0.0),
        "right": _frac("D1_PICK_VISION_CROP_RIGHT_FRAC", 0.0),
    }


def load_saved_crop_fracs() -> dict[str, float] | None:
    if not _CROP_PATH.is_file():
        return None
    try:
        data = json.loads(_CROP_PATH.read_text(encoding="utf-8"))
        fr = data.get("crop_fracs")
        if not isinstance(fr, dict):
            return None
        return {
            "top": _clamp_frac(fr.get("top", 0.0)),
            "bottom": _clamp_frac(fr.get("bottom", 0.30)),
            "left": _clamp_frac(fr.get("left", 0.0)),
            "right": _clamp_frac(fr.get("right", 0.0)),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def save_crop_fracs(crop_fracs: dict[str, Any]) -> dict[str, Any]:
    fr = {
        "top": _clamp_frac(crop_fracs.get("top", 0.0)),
        "bottom": _clamp_frac(crop_fracs.get("bottom", 0.30)),
        "left": _clamp_frac(crop_fracs.get("left", 0.0)),
        "right": _clamp_frac(crop_fracs.get("right", 0.0)),
    }
    _CROP_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "crop_fracs": fr,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _CROP_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def crop_settings_info() -> dict[str, Any]:
    saved = load_saved_crop_fracs()
    env = _env_default_fracs()
    active = saved if saved is not None else env
    return {
        "ok": True,
        "crop_fracs": active,
        "saved_crop_fracs": saved,
        "env_defaults": env,
        "has_saved": saved is not None,
        "path": str(_CROP_PATH),
    }


def vision_crop_fracs() -> dict[str, float]:
    saved = load_saved_crop_fracs()
    if saved is not None:
        return saved
    return _env_default_fracs()


def crop_frame_for_detection(frame: Any) -> tuple[Any, tuple[int, int], tuple[int, int, int, int]]:
    """Ritorna (crop, (ox, oy), (x1,y1,x2,y2) ROI su frame pieno)."""
    h, w = int(frame.shape[0]), int(frame.shape[1])
    fr = vision_crop_fracs()
    y1 = int(h * fr["top"])
    y2 = int(h * (1.0 - fr["bottom"]))
    x1 = int(w * fr["left"])
    x2 = int(w * (1.0 - fr["right"]))
    y2 = max(y1 + 32, min(h, y2))
    x2 = max(x1 + 32, min(w, x2))
    crop = frame[y1:y2, x1:x2].copy()
    return crop, (x1, y1), (x1, y1, x2, y2)


def map_detection_to_full_frame(
    det: dict[str, Any],
    *,
    offset_xy: tuple[int, int],
    crop_hw: tuple[int, int],
    full_hw: tuple[int, int],
) -> dict[str, Any]:
    """Riporta bbox/centro dal crop alle coordinate dell'immagine intera."""
    if not det:
        return det
    ox, oy = offset_xy
    ch, cw = crop_hw
    fh, fw = full_hw
    out = dict(det)
    xyxy = out.get("bbox_xyxy") or []
    if len(xyxy) >= 4:
        x1, y1, x2, y2 = [float(v) for v in xyxy[:4]]
        out["bbox_xyxy"] = [
            round(x1 + ox, 1),
            round(y1 + oy, 1),
            round(x2 + ox, 1),
            round(y2 + oy, 1),
        ]
    gcx, gcy = out.get("grip_center_px") or out.get("bbox_center_px") or [0, 0]
    gcx_f = float(gcx) + ox
    gcy_f = float(gcy) + oy
    out["grip_center_px"] = [round(gcx_f, 1), round(gcy_f, 1)]
    out["bbox_center_px"] = out["grip_center_px"]
    out["norm"] = [
        round((gcx_f - fw / 2.0) / max(fw / 2.0, 1.0), 4),
        round((gcy_f - fh / 2.0) / max(fh / 2.0, 1.0), 4),
    ]
    if out.get("bbox_xyxy"):
        x1, y1, x2, y2 = out["bbox_xyxy"]
        out["bbox_size_px"] = [round(x2 - x1, 1), round(y2 - y1, 1)]
        out["bbox_area_px"] = round((x2 - x1) * (y2 - y1), 1)
        out["bbox_area_ratio"] = round(out["bbox_area_px"] / max(fw * fh, 1), 5)
    for axis_key in ("orient_axis_px", "grip_align_axis_px"):
        axis = out.get(axis_key)
        if isinstance(axis, list) and len(axis) >= 2:
            mapped: list[list[float]] = []
            for pt in axis[:2]:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    mapped.append([round(float(pt[0]) + ox, 1), round(float(pt[1]) + oy, 1)])
            if len(mapped) == 2:
                out[axis_key] = mapped
    obox = out.get("orient_box_px")
    if isinstance(obox, list) and obox:
        mapped_box: list[list[float]] = []
        for pt in obox:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                mapped_box.append([round(float(pt[0]) + ox, 1), round(float(pt[1]) + oy, 1)])
        if mapped_box:
            out["orient_box_px"] = mapped_box
    out["vision_crop"] = {
        "offset_xy": [ox, oy],
        "crop_size_px": [cw, ch],
        "full_size_px": [fw, fh],
        "crop_fracs": vision_crop_fracs(),
    }
    return out


def draw_crop_roi_outline(frame: Any, roi: tuple[int, int, int, int]) -> Any:
    import cv2

    out = frame.copy()
    x1, y1, x2, y2 = roi
    h, w = out.shape[:2]
    overlay = out.copy()
    cv2.rectangle(overlay, (0, y2), (w, h), (40, 40, 40), -1)
    if y1 > 0:
        cv2.rectangle(overlay, (0, 0), (w, y1), (40, 40, 40), -1)
    if x1 > 0:
        cv2.rectangle(overlay, (0, 0), (x1, h), (40, 40, 40), -1)
    if x2 < w:
        cv2.rectangle(overlay, (x2, 0), (w, h), (40, 40, 40), -1)
    out = cv2.addWeighted(overlay, 0.45, out, 0.55, 0)
    cv2.rectangle(out, (x1, y1), (x2, y2), (180, 180, 255), 2)
    cv2.putText(
        out,
        "ROI rilevamento",
        (x1 + 4, max(18, y1 + 16)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (200, 200, 255),
        1,
        cv2.LINE_AA,
    )
    return out

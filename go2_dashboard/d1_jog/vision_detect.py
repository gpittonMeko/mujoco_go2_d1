"""Rilevamento oggetto: fusione RGB+depth+AI (default) o solo differenza sfondo."""

from __future__ import annotations

import math
import os
import time
from typing import Any

import numpy as np


def _detect_backend() -> str:
    return os.environ.get("VISION_DETECT_BACKEND", "yolo").strip().lower()


def _yolo_usable() -> bool:
    try:
        from go2_dashboard.d1_jog import vision_yolo
        from go2_dashboard.paths import ensure_d1_scripts_on_sys_path

        if not vision_yolo.resolve_yolo_model_path():
            return False
        ensure_d1_scripts_on_sys_path()
        import ultralytics  # noqa: F401

        return True
    except Exception:
        return False


def _effective_backend() -> str:
    b = _detect_backend()
    if b == "auto":
        return "yolo" if _yolo_usable() else "bg_diff"
    if b in ("yolo", "ai", "dynamic") and not _yolo_usable():
        return "bg_diff"
    return b


def _yolo_backend() -> bool:
    return _effective_backend() in ("yolo", "ai", "dynamic")


def _fusion_backend() -> bool:
    return _effective_backend() in ("fusion", "merge", "multi", "fused")


def _peek_depth_mm() -> np.ndarray | None:
    try:
        from go2_dashboard import realsense_pyrs as rp

        peek = rp.peek_bundle()
        if peek:
            d = peek.get("depth_mm")
            if d is not None:
                return d
    except Exception:
        pass
    return None

_LAST: dict[str, Any] | None = None
_LAST_TS = 0.0
_SMOOTH_CENTER: tuple[float, float] | None = None
_BG_CALIB: np.ndarray | None = None
_BG_CALIB_TS = 0.0
_LAST_FG_MASK: np.ndarray | None = None


def reset_background() -> None:
    global _BG_CALIB, _BG_CALIB_TS, _SMOOTH_CENTER
    _BG_CALIB = None
    _BG_CALIB_TS = 0.0
    _SMOOTH_CENTER = None
    if _fusion_backend():
        try:
            from go2_dashboard.d1_jog import vision_fusion

            vision_fusion.reset_fusion_calibration()
        except Exception:
            pass


def calibrate_background(frame: np.ndarray) -> dict[str, Any]:
    """Memorizza il frame corrente come sfondo (tavolo vuoto, senza oggetto)."""
    global _BG_CALIB, _BG_CALIB_TS, _SMOOTH_CENTER
    _BG_CALIB = frame.copy()
    _BG_CALIB_TS = time.time()
    _SMOOTH_CENTER = None
    if _fusion_backend():
        from go2_dashboard.d1_jog import vision_fusion

        return vision_fusion.calibrate_fusion(frame, _peek_depth_mm())
    return {"ok": True, "message": "Sfondo calibrato — ora metti l'oggetto al centro."}


def _frame_from_jpeg(jpg: bytes, cv2: Any) -> np.ndarray | None:
    arr = np.frombuffer(jpg, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return None
    if len(frame.shape) == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    return frame


def _stream_kind(frame: np.ndarray, cv2: Any) -> str:
    if frame.ndim != 3 or frame.shape[2] < 3:
        return "ir_mono"
    d0 = cv2.absdiff(frame[:, :, 0], frame[:, :, 1])
    d1 = cv2.absdiff(frame[:, :, 1], frame[:, :, 2])
    c = float(cv2.mean(d0)[0] + cv2.mean(d1)[0])
    if c >= float(os.environ.get("GO2_REALSENSE_MIN_FRAME_CHROMA", "2.5")):
        return "rgb"
    return "ir_mono"


def _roi_rect(shape: tuple[int, ...]) -> tuple[int, int, int, int]:
    h, w = shape[:2]
    margin = float(os.environ.get("VISION_ROI_MARGIN", "0.16"))
    return (
        int(w * margin),
        int(h * margin),
        int(w * (1.0 - margin)),
        int(h * (1.0 - margin)),
    )


def _border_background(frame: np.ndarray) -> np.ndarray:
    """Stima colore tavolo dai bordi del frame (sfondo visibile ai margini)."""
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = _roi_rect(frame.shape)
    strips = [
        frame[0:y0, :].reshape(-1, 3),
        frame[y1:h, :].reshape(-1, 3),
        frame[:, 0:x0].reshape(-1, 3),
        frame[:, x1:w].reshape(-1, 3),
    ]
    samples = np.vstack(strips)
    med = np.median(samples, axis=0).astype(np.uint8)
    return np.full((h, w, 3), med, dtype=np.uint8)


def _foreground_mask_bgdiff(frame: np.ndarray, cv2: Any) -> tuple[np.ndarray, str]:
    """
    Maschera oggetto = differenza significativa rispetto allo sfondo (calibrato o bordi).
    Non usa soglia adattiva sul grigio (che confonde texture interne al riquadro).
    """
    global _BG_CALIB, _LAST_FG_MASK
    h, w = frame.shape[:2]

    if _BG_CALIB is not None and _BG_CALIB.shape == frame.shape:
        bg = _BG_CALIB
        src = "bg_calib"
    else:
        bg = _border_background(frame)
        src = "bg_bordi"

    # Differenza per canale (più robusta di solo grigio su RGB)
    diff = cv2.absdiff(frame, bg)
    diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    diff_gray = cv2.GaussianBlur(diff_gray, (5, 5), 0)

    thr = int(os.environ.get("VISION_BG_DIFF_THRESH", "28"))
    _, mask = cv2.threshold(diff_gray, thr, 255, cv2.THRESH_BINARY)

    # Togli rumore piccolo, chiudi buchi
    mk = int(os.environ.get("VISION_MORPH_K", "5")) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (mk, mk))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    x0, y0, x1, y1 = _roi_rect(frame.shape)
    roi = np.zeros((h, w), dtype=np.uint8)
    roi[y0:y1, x0:x1] = 255
    mask = cv2.bitwise_and(mask, roi)

    _LAST_FG_MASK = mask
    return mask, src


def _mask_border_touch_ratio(mask_roi: np.ndarray, x: int, y: int, bw: int, bh: int, pad: int = 4) -> float:
    """Frazione di pixel del blob che toccano il bordo ROI (spesso sfondo, non oggetto)."""
    h, w = mask_roi.shape[:2]
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(w, x + bw + pad), min(h, y + bh + pad)
    patch = mask_roi[y0:y1, x0:x1]
    if patch.size == 0:
        return 1.0
    border = np.zeros_like(patch)
    border[0, :] = 255
    border[-1, :] = 255
    border[:, 0] = 255
    border[:, -1] = 255
    fg = patch > 0
    if not np.any(fg):
        return 0.0
    touch = np.logical_and(fg, border > 0).sum()
    return float(touch) / float(fg.sum())


def _pick_blob_from_mask(
    mask: np.ndarray,
    frame_hw: tuple[int, int],
    cv2: Any,
) -> tuple[Any | None, str, dict[str, float] | None]:
    """Sceglie il componente connesso che sembra oggetto (non tutto il riquadro)."""
    h, w = frame_hw
    frame_area = float(h * w)
    cx_img, cy_img = w / 2.0, h / 2.0
    min_r = float(os.environ.get("VISION_CONTOUR_MIN_AREA_RATIO", "0.02"))
    max_r = float(os.environ.get("VISION_CONTOUR_MAX_AREA_RATIO", "0.42"))
    max_center = float(os.environ.get("VISION_MAX_CENTER_DIST", "0.38"))
    max_border_touch = float(os.environ.get("VISION_MAX_BORDER_TOUCH", "0.12"))
    min_fg_mean = float(os.environ.get("VISION_FG_MIN_MEAN", "90"))

    num, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    best_label = -1
    best_score = -1.0

    for lab in range(1, num):
        x, y, bw, bh, area = [int(v) for v in stats[lab]]
        if area < frame_area * min_r or area > frame_area * max_r:
            continue
        if bw < 18 or bh < 18:
            continue
        cx, cy = float(centroids[lab][0]), float(centroids[lab][1])
        dist_n = math.hypot(cx - cx_img, cy - cy_img) / math.hypot(w / 2.0, h / 2.0)
        if dist_n > max_center:
            continue
        touch = _mask_border_touch_ratio(mask, x, y, bw, bh)
        if touch > max_border_touch:
            continue
        patch = mask[y : y + bh, x : x + bw]
        fg_mean = float(np.mean(patch)) if patch.size else 0.0
        if fg_mean < min_fg_mean:
            continue
        compact = float(area) / max(float(bw * bh), 1.0)
        centrality = max(0.05, 1.0 - dist_n / max(max_center, 0.05))
        score = float(area) * (compact**1.2) * (centrality**3) * (fg_mean / 255.0)
        if score > best_score:
            best_score = score
            best_label = lab

    if best_label < 0:
        return None, "nessun_blob_vs_sfondo", None

    blob_mask = (labels == best_label).astype(np.uint8) * 255
    contours, _ = cv2.findContours(blob_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, "contorno_vuoto", None
    cnt = max(contours, key=cv2.contourArea)
    st = stats[best_label]
    meta = {
        "fg_mean": round(float(np.mean(mask[st[1] : st[1] + st[3], st[0] : st[0] + st[2]])), 1),
        "border_touch": round(_mask_border_touch_ratio(mask, int(st[0]), int(st[1]), int(st[2]), int(st[3])), 3),
    }
    return cnt, "ok", meta


def _smooth_center(cx: float, cy: float) -> tuple[float, float]:
    global _SMOOTH_CENTER
    alpha = float(os.environ.get("VISION_SMOOTH_ALPHA", "0.45"))
    if _SMOOTH_CENTER is None:
        _SMOOTH_CENTER = (cx, cy)
    else:
        px, py = _SMOOTH_CENTER
        _SMOOTH_CENTER = (alpha * cx + (1.0 - alpha) * px, alpha * cy + (1.0 - alpha) * py)
    return _SMOOTH_CENTER


def _estimate_base_xyz_m(cx: float, cy: float, bbox_w: float, frame_hw: tuple[int, int]) -> list[float]:
    h, w = float(frame_hw[0]), float(frame_hw[1])
    box_w_m = float(os.environ.get("VISION_ASSUMED_WIDTH_M", "0.08"))
    fx = float(os.environ.get("REALSENSE_FX", w * 0.96))
    depth = float(np.clip((box_w_m * fx) / max(bbox_w, 25.0), 0.22, 0.55))
    lat = float(np.clip(((cx - w / 2.0) / max(w / 2.0, 1.0)) * 0.28 * depth, -0.32, 0.32))
    z = float(os.environ.get("VISION_PICK_Z_M", "0.14"))
    return [round(depth, 4), round(lat, 4), round(z, 4)]


def detect_contour_pick(frame: np.ndarray, cv2: Any) -> dict[str, Any]:
    t0 = time.perf_counter()
    h, w = frame.shape[:2]
    kind = _stream_kind(frame, cv2)

    mask, bg_src = _foreground_mask_bgdiff(frame, cv2)
    best_contour, reason, meta = _pick_blob_from_mask(mask, (h, w), cv2)

    if best_contour is None:
        hint = (
            "Metti il tavolo vuoto e premi «Calibra sfondo», poi posiziona l'oggetto al centro. "
            "Serve contrasto netto oggetto/sfondo (colore o luminosità diversa)."
        )
        if _BG_CALIB is None:
            hint = "Prima «Calibra sfondo» senza oggetto in scena, poi aggiungi l'oggetto al centro."
        return {
            "ok": False,
            "backend": "bg_diff",
            "detect_mode": "differenza_sfondo",
            "stream_kind": kind,
            "bg_source": bg_src,
            "reason": reason,
            "hint_it": hint,
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }

    m = cv2.moments(best_contour)
    cx = float(m["m10"] / m["m00"])
    cy = float(m["m01"] / m["m00"])
    cx, cy = _smooth_center(cx, cy)
    x, y, bw, bh = cv2.boundingRect(best_contour)
    area = float(cv2.contourArea(best_contour))

    enable_xyz = os.environ.get("VISION_ENABLE_XYZ", "1").lower() in {"1", "true", "yes"}
    base_xyz = _estimate_base_xyz_m(cx, cy, float(bw), (h, w)) if enable_xyz else None

    return {
        "ok": True,
        "backend": "bg_diff",
        "detect_mode": "differenza_sfondo",
        "stream_kind": kind,
        "bg_source": bg_src,
        "bg_calibrated": _BG_CALIB is not None,
        "mode": bg_src,
        "_contour": best_contour,
        "_fg_mask": mask,
        "center_px": [round(cx, 1), round(cy, 1)],
        "bbox_xyxy": [int(x), int(y), int(x + bw), int(y + bh)],
        "area_px": round(area, 1),
        "area_ratio": round(area / max(h * w, 1), 4),
        "blob_stats": meta,
        "base_xyz_m": base_xyz,
        "norm_from_image_center": [
            round((cx - w / 2.0) / max(w / 2.0, 1.0), 4),
            round((cy - h / 2.0) / max(h / 2.0, 1.0), 4),
        ],
        "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        "hint_it": "Oggetto = zona diversa dallo sfondo calibrato. Centro = punto di prelievo.",
    }


def draw_contour_overlay(frame: np.ndarray, det: dict[str, Any], cv2: Any) -> np.ndarray:
    out = frame.copy()
    h, w = out.shape[:2]
    cv2.drawMarker(out, (int(w / 2), int(h / 2)), (120, 120, 120), cv2.MARKER_CROSS, 18, 1)

    ch_masks = det.get("_channel_masks") or {}
    if ch_masks and os.environ.get("VISION_SHOW_CHANNEL_MASKS", "1").lower() in {"1", "true", "yes"}:
        if "depth" in ch_masks:
            tint_d = np.zeros_like(out)
            tint_d[:, :, 0] = (ch_masks["depth"] > 0).astype(np.uint8) * 50
            out = cv2.addWeighted(out, 1.0, tint_d, 0.25, 0)
        if "ai" in ch_masks:
            ai = det.get("ai") or {}
            bb = ai.get("bbox_xyxy")
            if bb and len(bb) >= 4:
                x1, y1, x2, y2 = [int(v) for v in bb[:4]]
                cv2.rectangle(out, (x1, y1), (x2, y2), (255, 120, 40), 1)
                lab = str(ai.get("label", "ai"))
                cv2.putText(out, lab[:20], (x1, max(14, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 160, 80), 1)

    fg = det.get("_fg_mask")
    if fg is not None and os.environ.get("VISION_SHOW_FG_MASK", "1").lower() in {"1", "true", "yes"}:
        tint = np.zeros_like(out)
        tint[:, :, 1] = (fg > 0).astype(np.uint8) * 90
        out = cv2.addWeighted(out, 1.0, tint, 0.38, 0)

    if not det.get("ok"):
        cv2.putText(out, det.get("reason", "?"), (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 80, 255), 2)
        hint = (det.get("hint_it") or "")[:52]
        if hint:
            cv2.putText(out, hint, (8, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 200, 255), 1)
        elif not det.get("bg_calibrated") and not _yolo_backend():
            cv2.putText(out, "Calibra sfondo", (8, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 200, 255), 2)
        return out

    cnt = det.get("_contour")
    if cnt is not None:
        cv2.drawContours(out, [cnt], -1, (40, 220, 80), 2)
    center = det.get("center_px") or []
    if len(center) >= 2:
        c = (int(round(center[0])), int(round(center[1])))
        cv2.circle(out, c, 9, (0, 255, 255), 2)
        cv2.drawMarker(out, c, (0, 255, 255), cv2.MARKER_CROSS, 22, 2)
    mode = det.get("detect_mode", "")
    if mode:
        cv2.putText(out, mode[:28], (8, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 220, 255), 1)
    return out


def run_detect_frame(frame: np.ndarray, cv2: Any) -> dict[str, Any]:
    global _LAST, _LAST_TS
    eff = _effective_backend()
    if eff in ("yolo", "ai", "dynamic"):
        from go2_dashboard.d1_jog import vision_yolo

        det = vision_yolo.detect_yolo_pick(frame, _peek_depth_mm(), cv2)
    elif _fusion_backend():
        from go2_dashboard.d1_jog import vision_fusion

        det = vision_fusion.detect_fused_pick(frame, _peek_depth_mm(), cv2)
    else:
        det = detect_contour_pick(frame, cv2)
    _LAST = det
    _LAST_TS = time.time()
    return det


def run_detect_jpeg(jpg: bytes, cv2: Any) -> dict[str, Any]:
    frame = _frame_from_jpeg(jpg, cv2)
    if frame is None:
        return {"ok": False, "error": "jpeg_decode_failed"}
    return run_detect_frame(frame, cv2)


def detector_stack_status() -> dict[str, Any]:
    eff = _effective_backend()
    yolo = eff in ("yolo", "ai", "dynamic")
    fusion = eff in ("fusion", "merge", "multi", "fused")
    rs_st: dict[str, Any] = {}
    ai_st: dict[str, Any] = {}
    yolo_path: str | None = None
    try:
        from go2_dashboard import realsense_pyrs as rp

        rs_st = rp.status()
    except Exception:
        pass
    if yolo or fusion:
        try:
            from go2_dashboard.d1_jog import vision_yolo
            from go2_dashboard.paths import ensure_d1_scripts_on_sys_path

            ensure_d1_scripts_on_sys_path()
            from box_object_detector import detector_status

            ai_st = detector_status()
            yolo_path = vision_yolo.resolve_yolo_model_path()
        except Exception as exc:
            ai_st = {"ok": False, "error": str(exc)}
    backend = eff
    mode = (
        "yolo_dinamico"
        if yolo
        else ("fusion_rgb_depth_ai" if fusion else "differenza_sfondo")
    )
    return {
        "ok": True,
        "backend": backend,
        "detect_mode": mode,
        "configured_backend": _detect_backend(),
        "yolo_usable": _yolo_usable(),
        "description": (
            "YOLO attivo (modello + ultralytics sulla NX)."
            if yolo
            else (
                "Merge RGB+depth+AI con voto tra canali."
                if fusion
                else "Differenza vs sfondo (calibra tavolo vuoto, poi oggetto al centro)."
            )
        ),
        "yolo_model_resolved": yolo_path,
        "bg_calibrated": _BG_CALIB is not None,
        "realsense_streams": rs_st.get("streams") or [],
        "ai_detector": ai_st,
        "calibrate_hint": (
            "Non necessario in modalità YOLO."
            if yolo
            else "Calibra solo per modalità bg_diff/fusion con diff. sfondo."
        ),
    }


def plan_summary(det: dict[str, Any]) -> dict[str, Any]:
    out = dict(det)
    out.pop("_contour", None)
    out.pop("_fg_mask", None)
    out.pop("_channel_masks", None)
    out.pop("_all_boxes", None)
    return out


def last_plan_summary() -> dict[str, Any]:
    if _LAST is None:
        return {"ok": False, "error": "no_detection_yet", "bg_calibrated": _BG_CALIB is not None}
    return plan_summary({**_LAST, "age_s": round(max(0.0, time.time() - _LAST_TS), 2)})


def encode_overlay_jpeg(jpg: bytes, cv2: Any, *, logical_camera: int) -> bytes | None:
    del logical_camera
    frame = _frame_from_jpeg(jpg, cv2)
    if frame is None:
        return None
    try:
        det = run_detect_frame(frame, cv2)
        if _effective_backend() in ("yolo", "ai", "dynamic"):
            from go2_dashboard.d1_jog import vision_yolo

            out = vision_yolo.draw_yolo_overlay(frame, det, cv2)
        else:
            out = draw_contour_overlay(frame, det, cv2)
        q = int(os.environ.get("VISION_DETECT_JPEG_QUALITY", "75"))
        ok, enc = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), max(55, min(90, q))])
        return enc.tobytes() if ok else None
    except Exception:
        return None


def run_plan_on_jpeg(jpg: bytes, cv2: Any, *, logical_camera: int) -> dict[str, Any]:
    del logical_camera
    return run_detect_jpeg(jpg, cv2)

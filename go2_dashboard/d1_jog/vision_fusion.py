"""Fusione RGB + depth + IR + detector AI (YOLO/fallback) per oggetto vs sfondo."""

from __future__ import annotations

import os
import time
from typing import Any

import numpy as np

from go2_dashboard.d1_jog import vision_detect
from go2_dashboard.paths import ensure_d1_scripts_on_sys_path

_DEPTH_BG: np.ndarray | None = None
_DEPTH_BG_TS = 0.0


def reset_fusion_calibration() -> None:
    global _DEPTH_BG, _DEPTH_BG_TS
    vision_detect.reset_background()
    _DEPTH_BG = None
    _DEPTH_BG_TS = 0.0


def calibrate_fusion(color: np.ndarray, depth_mm: np.ndarray | None) -> dict[str, Any]:
    out = vision_detect.calibrate_background(color)
    global _DEPTH_BG, _DEPTH_BG_TS
    if depth_mm is not None and depth_mm.shape[:2] == color.shape[:2]:
        _DEPTH_BG = depth_mm.copy()
        _DEPTH_BG_TS = time.time()
        out["depth_calibrated"] = True
    else:
        out["depth_calibrated"] = False
    out["message"] = "Sfondo RGB+depth calibrato — metti l'oggetto al centro."
    return out


def _roi_rect(shape: tuple[int, ...]) -> tuple[int, int, int, int]:
    return vision_detect._roi_rect(shape)


def _ai_detection(frame: np.ndarray) -> dict[str, Any]:
    ensure_d1_scripts_on_sys_path()
    try:
        from box_object_detector import detect_box_object, detector_status
    except Exception as exc:
        return {"ok": False, "backend": "import_error", "reason": str(exc)}
    det = detect_box_object(frame)
    det["detector_status"] = detector_status()
    return det


def _label_is_background(label: str) -> bool:
    reject = os.environ.get(
        "VISION_AI_REJECT_LABELS",
        "table,floor,desk,wall,person,chair,carpet,background,rug,sofa,bed",
    ).lower()
    tokens = {t.strip() for t in reject.replace(";", ",").split(",") if t.strip()}
    lab = (label or "").lower()
    return any(tok in lab for tok in tokens)


def _bbox_to_mask(
    shape: tuple[int, int],
    bbox_xyxy: list[float],
    cv2: Any,
    *,
    pad_ratio: float = 0.08,
) -> np.ndarray:
    h, w = shape
    mask = np.zeros((h, w), dtype=np.uint8)
    if len(bbox_xyxy) < 4:
        return mask
    x1, y1, x2, y2 = [float(v) for v in bbox_xyxy[:4]]
    bw, bh = x2 - x1, y2 - y1
    pad_x, pad_y = bw * pad_ratio, bh * pad_ratio
    x1 = int(max(0, x1 - pad_x))
    y1 = int(max(0, y1 - pad_y))
    x2 = int(min(w, x2 + pad_x))
    y2 = int(min(h, y2 + pad_y))
    mask[y1:y2, x1:x2] = 255
    mk = int(os.environ.get("VISION_AI_MASK_ERODE", "3")) | 1
    if mk > 1:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (mk, mk))
        mask = cv2.erode(mask, k, iterations=1)
    x0, y0, x1r, y1r = _roi_rect((h, w))
    roi = np.zeros((h, w), dtype=np.uint8)
    roi[y0:y1r, x0:x1r] = 255
    return cv2.bitwise_and(mask, roi)


def _depth_foreground_mask(depth_mm: np.ndarray, cv2: Any) -> tuple[np.ndarray, str]:
    global _DEPTH_BG
    h, w = depth_mm.shape[:2]
    valid = depth_mm > 0
    if not np.any(valid):
        return np.zeros((h, w), dtype=np.uint8), "depth_vuoto"

    if _DEPTH_BG is not None and _DEPTH_BG.shape == depth_mm.shape:
        bg = _DEPTH_BG.astype(np.float32)
        src = "depth_calib"
    else:
        x0, y0, x1, y1 = _roi_rect((h, w))
        border = np.concatenate(
            [
                depth_mm[0:y0, :][valid[0:y0, :]],
                depth_mm[y1:h, :][valid[y1:h, :]],
                depth_mm[:, 0:x0][valid[:, 0:x0]],
                depth_mm[:, x1:w][valid[:, x1:w]],
            ]
        )
        if border.size < 20:
            return np.zeros((h, w), dtype=np.uint8), "depth_bordi_insuff"
        med = float(np.median(border[border > 0]))
        bg = np.full((h, w), med, dtype=np.float32)
        src = "depth_bordi"

    diff_mm = np.clip(bg - depth_mm.astype(np.float32), 0.0, 2000.0)
    min_above = float(os.environ.get("VISION_DEPTH_MIN_ABOVE_MM", "12"))
    mask = (diff_mm >= min_above).astype(np.uint8) * 255
    mask[~valid] = 0

    mk = int(os.environ.get("VISION_MORPH_K", "5")) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (mk, mk))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

    x0, y0, x1, y1 = _roi_rect((h, w))
    roi = np.zeros((h, w), dtype=np.uint8)
    roi[y0:y1, x0:x1] = 255
    mask = cv2.bitwise_and(mask, roi)
    return mask, src


def _fuse_masks(
    masks: dict[str, np.ndarray],
    ai_ok: bool,
    cv2: Any,
) -> np.ndarray:
    """Unisce maschere: voto 2/3 o AND con gate AI se configurato."""
    if not masks:
        return np.zeros((480, 640), dtype=np.uint8)
    h, w = next(iter(masks.values())).shape[:2]
    stack = np.stack([(m > 0).astype(np.uint8) for m in masks.values()], axis=0)
    votes = stack.sum(axis=0)

    min_votes = int(os.environ.get("VISION_FUSION_MIN_VOTES", "2"))
    min_votes = min(min_votes, max(1, len(masks)))
    fused = (votes >= min_votes).astype(np.uint8) * 255

    if ai_ok and os.environ.get("VISION_AI_GATE", "1").lower() in {"1", "true", "yes"}:
        ai_m = masks.get("ai")
        if ai_m is not None:
            fused = cv2.bitwise_and(fused, ai_m)

    return fused


def build_fusion_masks(
    color: np.ndarray,
    depth_mm: np.ndarray | None,
    cv2: Any,
) -> dict[str, Any]:
    """Maschere per canale + maschera fusa."""
    t0 = time.perf_counter()
    color_mask, color_src = vision_detect._foreground_mask_bgdiff(color, cv2)
    masks: dict[str, np.ndarray] = {"color": color_mask}
    sources: dict[str, str] = {"color": color_src}

    depth_mask: np.ndarray | None = None
    depth_src = None
    if depth_mm is not None and depth_mm.shape[:2] == color.shape[:2]:
        depth_mask, depth_src = _depth_foreground_mask(depth_mm, cv2)
        masks["depth"] = depth_mask
        sources["depth"] = depth_src or ""

    ai_det = _ai_detection(color)
    ai_mask: np.ndarray | None = None
    ai_ok = False
    if ai_det.get("ok") and not _label_is_background(str(ai_det.get("label", ""))):
        conf_min = float(os.environ.get("VISION_AI_MIN_CONF", "0.22"))
        if float(ai_det.get("confidence", 0.0)) >= conf_min:
            ai_mask = _bbox_to_mask(color.shape[:2], ai_det.get("bbox_xyxy") or [], cv2)
            masks["ai"] = ai_mask
            ai_ok = True
    elif ai_det.get("ok") and _label_is_background(str(ai_det.get("label", ""))):
        ai_det["rejected_as_background"] = True

    require_ai = os.environ.get("VISION_AI_REQUIRED", "0").lower() in {"1", "true", "yes"}
    if require_ai and not ai_ok:
        fused = np.zeros(color.shape[:2], dtype=np.uint8)
    else:
        fused = _fuse_masks(masks, ai_ok, cv2)

    return {
        "masks": masks,
        "fused": fused,
        "sources": sources,
        "ai": ai_det,
        "ai_ok": ai_ok,
        "depth_src": depth_src,
        "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
    }


def detect_fused_pick(
    color: np.ndarray,
    depth_mm: np.ndarray | None,
    cv2: Any,
) -> dict[str, Any]:
    """Rilevamento merge: blob sulla maschera fusa + metadati canali."""
    h, w = color.shape[:2]
    kind = vision_detect._stream_kind(color, cv2)
    fusion = build_fusion_masks(color, depth_mm, cv2)
    fused = fusion["fused"]
    ai_det = fusion.get("ai") or {}

    if ai_det.get("rejected_as_background"):
        return {
            "ok": False,
            "backend": "fusion",
            "detect_mode": "fusion_rgb_depth_ai",
            "stream_kind": kind,
            "reason": "ai_background_class",
            "ai_label": ai_det.get("label"),
            "hint_it": "Il modello ha classificato lo sfondo — sposta l'oggetto o regola VISION_AI_REJECT_LABELS.",
            "fusion": {k: v for k, v in fusion.items() if k != "masks"},
        }

    cnt, reason, meta = vision_detect._pick_blob_from_mask(fused, (h, w), cv2)
    if cnt is None:
        hint = (
            "Calibra sfondo (tavolo vuoto), poi oggetto al centro. "
            "Servono almeno 2 segnali concordi (RGB+depth o RGB+AI)."
        )
        if vision_detect._BG_CALIB is None:
            hint = "Calibra sfondo senza oggetto; verifica depth/IR attivi in pyrealsense2."
        return {
            "ok": False,
            "backend": "fusion",
            "detect_mode": "fusion_rgb_depth_ai",
            "stream_kind": kind,
            "reason": reason,
            "hint_it": hint,
            "bg_calibrated": vision_detect._BG_CALIB is not None,
            "depth_calibrated": _DEPTH_BG is not None,
            "fusion_sources": fusion.get("sources"),
            "ai": {k: ai_det.get(k) for k in ("ok", "backend", "label", "confidence", "bbox_xyxy")},
            "latency_ms": fusion.get("latency_ms"),
        }

    m = cv2.moments(cnt)
    cx = float(m["m10"] / m["m00"])
    cy = float(m["m01"] / m["m00"])
    cx, cy = vision_detect._smooth_center(cx, cy)
    x, y, bw, bh = cv2.boundingRect(cnt)
    area = float(cv2.contourArea(cnt))

    enable_xyz = os.environ.get("VISION_ENABLE_XYZ", "1").lower() in {"1", "true", "yes"}
    base_xyz = None
    if enable_xyz:
        if depth_mm is not None and 0 <= int(cy) < h and 0 <= int(cx) < w:
            z_mm = float(depth_mm[int(cy), int(cx)])
            if z_mm > 0:
                fx = float(os.environ.get("REALSENSE_FX", w * 0.96))
                z_m = z_mm / 1000.0
                lat = ((cx - w / 2.0) / fx) * z_m
                lift = float(os.environ.get("VISION_PICK_Z_M", "0.14"))
                base_xyz = [round(z_m, 4), round(lat, 4), round(lift, 4)]
        if base_xyz is None:
            base_xyz = vision_detect._estimate_base_xyz_m(cx, cy, float(bw), (h, w))

    return {
        "ok": True,
        "backend": "fusion",
        "detect_mode": "fusion_rgb_depth_ai",
        "stream_kind": kind,
        "bg_calibrated": vision_detect._BG_CALIB is not None,
        "depth_calibrated": _DEPTH_BG is not None,
        "fusion_sources": fusion.get("sources"),
        "ai": {k: ai_det.get(k) for k in ("ok", "backend", "label", "confidence", "bbox_xyxy")},
        "ai_ok": fusion.get("ai_ok"),
        "_contour": cnt,
        "_fg_mask": fused,
        "_channel_masks": fusion.get("masks"),
        "center_px": [round(cx, 1), round(cy, 1)],
        "bbox_xyxy": [int(x), int(y), int(x + bw), int(y + bh)],
        "area_px": round(area, 1),
        "area_ratio": round(area / max(h * w, 1), 4),
        "blob_stats": meta,
        "base_xyz_m": base_xyz,
        "latency_ms": fusion.get("latency_ms"),
        "hint_it": "Merge RGB+depth+AI: oggetto = segnali concordi, AI filtra classi sfondo.",
    }

"""Rilevamento dinamico YOLO — nessuna calibrazione sfondo; pezzo già sul tavolo."""

from __future__ import annotations

import math
import os
import time
from pathlib import Path
from typing import Any

import numpy as np

from go2_dashboard.d1_jog import vision_detect
from go2_dashboard.paths import PROJECT_ROOT, ensure_d1_scripts_on_sys_path


def resolve_yolo_model_path() -> str | None:
    raw = os.environ.get("GO2_YOLO_MODEL", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_file():
            return str(p)
    for rel in (
        "models/yolov8n.pt",
        "models/yolo11n.pt",
        "models/yolov8n.engine",
        "models/yolo11n.engine",
        "models/yolov8n.onnx",
        "data/yolov8n.pt",
        "yolov8n.pt",
    ):
        p = PROJECT_ROOT / rel
        if p.is_file():
            return str(p)
    return None


def _label_is_background(label: str) -> bool:
    reject = os.environ.get(
        "VISION_AI_REJECT_LABELS",
        "table,floor,desk,wall,person,chair,carpet,background,rug,sofa,bed,dining table",
    ).lower()
    tokens = {t.strip() for t in reject.replace(";", ",").split(",") if t.strip()}
    lab = (label or "").lower()
    return any(tok in lab for tok in tokens)


def _target_labels() -> set[str] | None:
    raw = os.environ.get("VISION_YOLO_TARGET_LABELS", "").strip().lower()
    if not raw:
        return None
    return {t.strip() for t in raw.replace(";", ",").split(",") if t.strip()}


def _label_allowed(label: str) -> bool:
    if _label_is_background(label):
        return False
    targets = _target_labels()
    if targets is None:
        return True
    lab = (label or "").lower()
    return any(t in lab or lab in t for t in targets)


def _score_box(box: dict[str, Any], w: int, h: int) -> float:
    xy = box.get("bbox_xyxy") or []
    if len(xy) < 4:
        return -1.0
    x1, y1, x2, y2 = [float(v) for v in xy[:4]]
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    dist = math.hypot(cx - w / 2.0, cy - h / 2.0) / math.hypot(w / 2.0, h / 2.0, 1.0)
    area = max(0.0, x2 - x1) * max(0.0, y2 - y1) / max(w * h, 1.0)
    conf = float(box.get("confidence", 0.0))
    centrality = max(0.05, 1.0 - dist / 0.55)
    return conf * (area**0.85) * (centrality**2.5)


def _bbox_contour(xyxy: list[float], cv2: Any) -> np.ndarray:
    x1, y1, x2, y2 = [int(v) for v in xyxy[:4]]
    return np.array(
        [[[x1, y1]], [[x2, y1]], [[x2, y2]], [[x1, y2]]],
        dtype=np.int32,
    )


def _pick_best_box(boxes: list[dict[str, Any]], frame_hw: tuple[int, int]) -> dict[str, Any] | None:
    h, w = frame_hw
    conf_min = float(os.environ.get("VISION_AI_MIN_CONF", os.environ.get("GO2_YOLO_CONF", "0.25")))
    best: dict[str, Any] | None = None
    best_sc = -1.0
    for box in boxes:
        if float(box.get("confidence", 0.0)) < conf_min:
            continue
        if not _label_allowed(str(box.get("label", ""))):
            continue
        sc = _score_box(box, w, h)
        if sc > best_sc:
            best_sc = sc
            best = box
    return best


def detect_yolo_pick(
    color: np.ndarray,
    depth_mm: np.ndarray | None,
    cv2: Any,
) -> dict[str, Any]:
    """Centro prelievo da bbox YOLO — funziona con oggetto già presente."""
    t0 = time.perf_counter()
    h, w = color.shape[:2]
    kind = vision_detect._stream_kind(color, cv2)
    ensure_d1_scripts_on_sys_path()

    model_path = resolve_yolo_model_path()
    if model_path and not os.environ.get("GO2_YOLO_MODEL", "").strip():
        os.environ["GO2_YOLO_MODEL"] = model_path

    try:
        from box_object_detector import detect_all_objects, detector_status
    except Exception as exc:
        return {
            "ok": False,
            "backend": "yolo",
            "detect_mode": "yolo_dinamico",
            "stream_kind": kind,
            "reason": "import_error",
            "hint_it": f"Detector non disponibile: {exc}",
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }

    all_det = detect_all_objects(color)
    boxes = list(all_det.get("boxes") or [])
    best = _pick_best_box(boxes, (h, w))
    ai_meta = {
        "ok": best is not None,
        "backend": all_det.get("backend"),
        "model_path": model_path or all_det.get("model_path"),
        "all_count": len(boxes),
        "detector_status": detector_status(),
    }

    if best is None:
        hint = (
            "Nessun oggetto COCO riconosciuto — abbassa GO2_YOLO_CONF o metti un oggetto tipico "
            "(bottle, cup, cell phone, book, …) al centro."
        )
        if not model_path:
            hint = "Modello mancante: bash scripts/nx_install_yolo_vision.sh (scarica yolov8n.pt)"
        elif all_det.get("reason"):
            r = str(all_det.get("reason", ""))
            if "C3k2" in r:
                hint = (
                    "Modello yolo11n non compatibile con ultralytics sulla NX. "
                    "Usa models/yolov8n.pt: bash scripts/nx_install_yolo_vision.sh"
                )
            else:
                hint = f"YOLO: {r[:120]}"
        rejected = [
            f"{b.get('label')} {b.get('confidence')}"
            for b in boxes[:8]
            if not _label_allowed(str(b.get("label", "")))
        ]
        allowed_n = sum(1 for b in boxes if _label_allowed(str(b.get("label", ""))))
        if boxes and allowed_n == 0:
            hint = "YOLO vede solo sfondo/tavolo — etichette: " + ", ".join(rejected[:5])
        elif boxes:
            hint = f"YOLO: {len(boxes)} box ma sotto soglia — prova GO2_YOLO_CONF=0.15"
        return {
            "ok": False,
            "backend": "yolo",
            "detect_mode": "yolo_dinamico",
            "stream_kind": kind,
            "reason": "no_yolo_detection",
            "hint_it": hint,
            "ai": {**ai_meta, "seen_boxes": len(boxes), "rejected_preview": rejected[:5]},
            "_all_boxes": boxes,
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }

    xy = best["bbox_xyxy"]
    x1, y1, x2, y2 = [float(v) for v in xy[:4]]
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0
    cx, cy = vision_detect._smooth_center(cx, cy)
    cnt = _bbox_contour(xy, cv2)
    bw, bh = x2 - x1, y2 - y1
    area = float(bw * bh)
    ai_mask = np.zeros((h, w), dtype=np.uint8)
    ai_mask[int(y1) : int(y2), int(x1) : int(x2)] = 255

    enable_xyz = os.environ.get("VISION_ENABLE_XYZ", "1").lower() in {"1", "true", "yes"}
    base_xyz = None
    if enable_xyz and depth_mm is not None and depth_mm.shape[:2] == (h, w):
        z_mm = float(depth_mm[int(cy), int(cx)])
        if z_mm > 0:
            fx = float(os.environ.get("REALSENSE_FX", w * 0.96))
            z_m = z_mm / 1000.0
            lat = ((cx - w / 2.0) / fx) * z_m
            lift = float(os.environ.get("VISION_PICK_Z_M", "0.14"))
            base_xyz = [round(z_m, 4), round(lat, 4), round(lift, 4)]
    if base_xyz is None and enable_xyz:
        base_xyz = vision_detect._estimate_base_xyz_m(cx, cy, float(bw), (h, w))

    return {
        "ok": True,
        "backend": "yolo",
        "detect_mode": "yolo_dinamico",
        "stream_kind": kind,
        "ai": {
            **ai_meta,
            "label": best.get("label"),
            "confidence": best.get("confidence"),
            "bbox_xyxy": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
        },
        "ai_ok": True,
        "_contour": cnt,
        "_fg_mask": ai_mask,
        "_all_boxes": boxes,
        "center_px": [round(cx, 1), round(cy, 1)],
        "bbox_xyxy": [int(x1), int(y1), int(x2), int(y2)],
        "area_px": round(area, 1),
        "area_ratio": round(area / max(h * w, 1), 4),
        "base_xyz_m": base_xyz,
        "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        "hint_it": "Rilevamento YOLO live — oggetto già in scena OK.",
    }


def draw_yolo_overlay(frame: np.ndarray, det: dict[str, Any], cv2: Any) -> np.ndarray:
    out = frame.copy()
    h, w = out.shape[:2]
    for box in det.get("_all_boxes") or []:
        xy = box.get("bbox_xyxy") or []
        if len(xy) < 4:
            continue
        x1, y1, x2, y2 = [int(v) for v in xy[:4]]
        col = (80, 80, 80)
        if _label_allowed(str(box.get("label", ""))):
            col = (60, 180, 255)
        cv2.rectangle(out, (x1, y1), (x2, y2), col, 1)
        cv2.putText(
            out,
            f"{box.get('label','?')[:12]} {box.get('confidence',0):.2f}",
            (x1, max(12, y1 - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            col,
            1,
        )
    return vision_detect.draw_contour_overlay(out, det, cv2)

#!/usr/bin/env python3
"""
Optional box detector for the Go2+D1 grasp dashboard.

The hot path prefers an exported Ultralytics model (TensorRT .engine, ONNX, or
.pt if explicitly configured). If no model is present, a conservative OpenCV
proposal can provide a bbox for lab bring-up; the UI reports that fallback
clearly so it is not confused with a trained detector.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

_MODEL_CACHE: dict[str, Any] = {}


def detector_status() -> dict[str, Any]:
    model_path = os.environ.get("GO2_YOLO_MODEL", "").strip()
    p = Path(model_path).expanduser() if model_path else None
    return {
        "ok": True,
        "backend_preference": "tensorrt/onnx/ultralytics",
        "model_path": str(p) if p else None,
        "model_exists": bool(p and p.is_file()),
        "classic_fallback_enabled": os.environ.get("GO2_CLASSIC_BOX_FALLBACK", "1").lower()
        in {"1", "true", "yes"},
        "recommended_models": ["YOLO26n TensorRT FP16", "YOLO11n TensorRT FP16"],
    }


def _load_ultralytics_model(path: str) -> Any:
    cached = _MODEL_CACHE.get(path)
    if cached is not None:
        return cached
    from ultralytics import YOLO  # type: ignore

    model = YOLO(path)
    _MODEL_CACHE[path] = model
    return model


def _detect_ultralytics(frame: np.ndarray, model_path: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    model = _load_ultralytics_model(model_path)
    imgsz = int(os.environ.get("GO2_YOLO_IMGSZ", "640"))
    conf_min = float(os.environ.get("GO2_YOLO_CONF", "0.30"))
    results = model.predict(frame, imgsz=imgsz, conf=conf_min, verbose=False)
    boxes = []
    for result in results:
        names = getattr(result, "names", {}) or {}
        rb = getattr(result, "boxes", None)
        if rb is None:
            continue
        for b in rb:
            xyxy = [float(x) for x in b.xyxy[0].tolist()]
            conf = float(b.conf[0]) if getattr(b, "conf", None) is not None else 0.0
            cls_i = int(b.cls[0]) if getattr(b, "cls", None) is not None else -1
            label = str(names.get(cls_i, cls_i))
            boxes.append({"bbox_xyxy": xyxy, "confidence": round(conf, 4), "class_id": cls_i, "label": label})
    return _select_detection(boxes, frame.shape[:2], "ultralytics", time.perf_counter() - t0)


def _classic_box_proposal(frame: np.ndarray) -> dict[str, Any]:
    t0 = time.perf_counter()
    h, w = frame.shape[:2]
    blur = cv2.GaussianBlur(frame, (5, 5), 0)
    gray = cv2.cvtColor(blur, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 45, 135)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    min_area = float(os.environ.get("GO2_CLASSIC_BOX_MIN_AREA_PX", str(w * h * 0.015)))
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        area = float(bw * bh)
        if area < min_area:
            continue
        aspect = bw / max(bh, 1)
        if not (0.35 <= aspect <= 3.2):
            continue
        # Prefer central, medium-large rectangular proposals.
        cx = x + bw / 2.0
        cy = y + bh / 2.0
        center_penalty = abs(cx - w / 2.0) / max(w / 2.0, 1.0) + abs(cy - h / 2.0) / max(h / 2.0, 1.0)
        score = area / max(w * h, 1) - 0.08 * center_penalty
        boxes.append(
            {
                "bbox_xyxy": [float(x), float(y), float(x + bw), float(y + bh)],
                "confidence": round(max(0.05, min(0.55, score)), 4),
                "class_id": -1,
                "label": "box_proposal",
            }
        )
    return _select_detection(boxes, frame.shape[:2], "classic_contour_fallback", time.perf_counter() - t0)


def _select_detection(
    boxes: list[dict[str, Any]],
    frame_hw: tuple[int, int],
    backend: str,
    elapsed_s: float,
) -> dict[str, Any]:
    h, w = frame_hw
    if not boxes:
        return {
            "ok": False,
            "backend": backend,
            "reason": "no_box_detection",
            "latency_ms": round(elapsed_s * 1000.0, 2),
        }
    boxes = sorted(
        boxes,
        key=lambda b: (float(b.get("confidence", 0.0)), _bbox_area(b.get("bbox_xyxy") or [])),
        reverse=True,
    )
    det = boxes[0]
    x1, y1, x2, y2 = [float(v) for v in det["bbox_xyxy"]]
    bw = max(1.0, x2 - x1)
    bh = max(1.0, y2 - y1)
    cx = x1 + bw / 2.0
    cy = y1 + bh / 2.0
    nx = (cx - w / 2.0) / max(w / 2.0, 1.0)
    ny = (cy - h / 2.0) / max(h / 2.0, 1.0)
    area = bw * bh
    return {
        "ok": True,
        "backend": backend,
        "label": det.get("label", "box"),
        "confidence": det.get("confidence", 0.0),
        "bbox_xyxy": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
        "bbox_center_px": [round(cx, 1), round(cy, 1)],
        "bbox_size_px": [round(bw, 1), round(bh, 1)],
        "bbox_area_px": round(area, 1),
        "bbox_area_ratio": round(area / max(w * h, 1), 5),
        "norm": [round(nx, 4), round(ny, 4)],
        "grip_center_px": [round(cx, 1), round(cy, 1)],
        "grip_axis_px": [[round(x1, 1), round(cy, 1)], [round(x2, 1), round(cy, 1)]],
        "gripper_model": "east_west_close_to_center",
        "latency_ms": round(elapsed_s * 1000.0, 2),
        "all_count": len(boxes),
    }


def _bbox_area(xyxy: list[float]) -> float:
    if len(xyxy) < 4:
        return 0.0
    return max(0.0, float(xyxy[2]) - float(xyxy[0])) * max(0.0, float(xyxy[3]) - float(xyxy[1]))


def detect_box_object(frame: np.ndarray) -> dict[str, Any]:
    model_path = os.environ.get("GO2_YOLO_MODEL", "").strip()
    if model_path:
        p = Path(model_path).expanduser()
        if p.is_file():
            try:
                out = _detect_ultralytics(frame, str(p))
                out["model_path"] = str(p)
                return out
            except Exception as exc:
                if os.environ.get("GO2_CLASSIC_BOX_FALLBACK", "1").lower() not in {"1", "true", "yes"}:
                    return {"ok": False, "backend": "ultralytics", "reason": repr(exc), "model_path": str(p)}
        elif os.environ.get("GO2_CLASSIC_BOX_FALLBACK", "1").lower() not in {"1", "true", "yes"}:
            return {"ok": False, "backend": "disabled", "reason": f"GO2_YOLO_MODEL missing: {p}"}

    if os.environ.get("GO2_CLASSIC_BOX_FALLBACK", "1").lower() in {"1", "true", "yes"}:
        return _classic_box_proposal(frame)
    return {"ok": False, "backend": "disabled", "reason": "No GO2_YOLO_MODEL and classic fallback disabled"}

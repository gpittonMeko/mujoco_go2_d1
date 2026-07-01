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
_MODEL_META_CACHE: dict[str, dict[str, Any]] = {}


def _infer_model_family(model_path: str | None) -> str | None:
    if not model_path:
        return None
    name = Path(model_path).name.lower()
    if "world" in name:
        return "yolo_world"
    if "ground" in name and "dino" in name:
        return "grounding_dino"
    if "owl" in name:
        return "owl"
    if name.endswith('.engine'):
        return "tensorrt_engine"
    if name.endswith('.onnx'):
        return "onnx_detector"
    if name.endswith('.pt'):
        return "ultralytics_pt"
    return "unknown"


def _infer_training_scope(model_family: str | None, labels: list[str]) -> tuple[str, bool, str]:
    fam = (model_family or "").lower()
    if fam == "yolo_world" or fam in {"grounding_dino", "owl"}:
        return (
            "open_vocabulary_text_prompted",
            True,
            "Usalo per ricerca semantica 2D; per grasp 3D vero servono depth/pose/grasp separati.",
        )
    if labels:
        return (
            "closed_set_labels",
            False,
            "Modello chiuso: puoi usare con buona confidenza solo le classi elencate in trained_labels.",
        )
    return (
        "heuristic_only",
        False,
        "Nessun modello oggetti configurato: resta solo il fallback 2D euristico per preview/debug.",
    )


def _model_metadata(model_path: str | None) -> dict[str, Any]:
    if not model_path:
        scope, open_vocab, note = _infer_training_scope(None, [])
        return {
            "model_family": None,
            "trained_labels": [],
            "training_scope": scope,
            "open_vocabulary": open_vocab,
            "recommended_use_it": note,
        }
    cached = _MODEL_META_CACHE.get(model_path)
    if cached is not None:
        return dict(cached)
    labels: list[str] = []
    family = _infer_model_family(model_path)
    p = Path(model_path).expanduser()
    if p.is_file() and p.suffix.lower() in {".pt", ".engine", ".onnx"}:
        try:
            model = _load_ultralytics_model(str(p))
            names = getattr(model, "names", {}) or {}
            if isinstance(names, dict):
                labels = [str(v) for _, v in sorted(names.items(), key=lambda kv: kv[0])]
            elif isinstance(names, (list, tuple)):
                labels = [str(v) for v in names]
        except Exception:
            labels = []
    scope, open_vocab, note = _infer_training_scope(family, labels)
    meta = {
        "model_family": family,
        "trained_labels": labels,
        "training_scope": scope,
        "open_vocabulary": open_vocab,
        "recommended_use_it": note,
    }
    _MODEL_META_CACHE[model_path] = dict(meta)
    return meta


def detector_status() -> dict[str, Any]:
    model_path = os.environ.get("GO2_YOLO_MODEL", "").strip()
    p = Path(model_path).expanduser() if model_path else None
    meta = _model_metadata(str(p) if p else None)
    backend = _pick_detect_backend()
    color_only = _color_only_pick_detect()
    return {
        "ok": True,
        "pick_detect_backend": backend,
        "color_only": color_only,
        "backend_preference": "color_blue_box" if color_only or backend.startswith("color") else "tensorrt/onnx/ultralytics",
        "model_path": str(p) if p else None,
        "model_exists": bool(p and p.is_file()),
        "classic_fallback_enabled": os.environ.get("GO2_CLASSIC_BOX_FALLBACK", "1").lower()
        in {"1", "true", "yes"},
        "color_hsv": {
            "h_min": _parse_int_env("D1_COLOR_BOX_H_MIN", 95),
            "h_max": _parse_int_env("D1_COLOR_BOX_H_MAX", 130),
            "s_min": _parse_int_env("D1_COLOR_BOX_S_MIN", 45),
            "v_min": _parse_int_env("D1_COLOR_BOX_V_MIN", 35),
        },
        "recommended_models": ["YOLO-World small TensorRT FP16", "YOLO11n TensorRT FP16"],
        **meta,
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
    return _select_detection(boxes, frame, "ultralytics", time.perf_counter() - t0)


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
    return _select_detection(boxes, frame, "classic_contour_fallback", time.perf_counter() - t0)


def _pick_detect_backend() -> str:
    return (os.environ.get("D1_PICK_DETECT_BACKEND") or "color").strip().lower()


def _parse_int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_float_env(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _normalize_angle_deg(angle: float) -> float:
    """Porta l'angolo in [-90, 90] (asse lungo del pezzo)."""
    a = float(angle)
    while a <= -90.0:
        a += 180.0
    while a > 90.0:
        a -= 180.0
    return round(a, 2)


def _angle_from_min_area_rect(rw: float, rh: float, angle: float) -> float:
    """Angolo asse lungo da cv2.minAreaRect (stessa convenzione OpenCV)."""
    if rw < 1.0 or rh < 1.0:
        return 0.0
    a = float(angle)
    if rw < rh:
        a += 90.0
    return _normalize_angle_deg(a)


def _shortest_angle_delta_deg(a: float, b: float) -> float:
    d = float(a) - float(b)
    while d > 90.0:
        d -= 180.0
    while d < -90.0:
        d += 180.0
    return d


def _circular_mean_deg(a: float, b: float, *, wa: float = 0.6, wb: float = 0.4) -> float:
    import math

    ra = math.radians(float(a))
    rb = math.radians(float(b))
    x = wa * math.cos(ra) + wb * math.cos(rb)
    y = wa * math.sin(ra) + wb * math.sin(rb)
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return _normalize_angle_deg(a)
    return _normalize_angle_deg(math.degrees(math.atan2(y, x)))


def _long_edge_angle_from_box_pts(box_pts: np.ndarray | list[Any]) -> float | None:
    """Angolo del lato più lungo del box orientato (più stabile vicino a ±90°)."""
    import math

    pts = np.asarray(box_pts, dtype=np.float64).reshape(-1, 2)
    if pts.shape[0] < 4:
        return None
    best_len = 0.0
    best_ang: float | None = None
    for i in range(4):
        p0 = pts[i]
        p1 = pts[(i + 1) % 4]
        dx = float(p1[0] - p0[0])
        dy = float(p1[1] - p0[1])
        ln = math.hypot(dx, dy)
        if ln <= best_len:
            continue
        best_len = ln
        best_ang = math.degrees(math.atan2(dy, dx))
    if best_ang is None:
        return None
    return _normalize_angle_deg(best_ang)


def _orientation_from_mask_pca(mask: np.ndarray, cnt: np.ndarray) -> float | None:
    """Asse principale del blob blu (PCA) — meno salti di minAreaRect."""
    import math

    pts = cv2.findNonZero(mask)
    if pts is None or len(pts) < 24:
        if cnt is None or len(cnt) < 5:
            return None
        pts = cnt.reshape(-1, 1, 2)
    pts2 = pts.reshape(-1, 2).astype(np.float64)
    if pts2.shape[0] < 24:
        return None
    mean = pts2.mean(axis=0)
    centered = pts2 - mean
    cov = np.cov(centered.T)
    if cov.shape != (2, 2):
        return None
    evals, evecs = np.linalg.eigh(cov)
    major = evecs[:, int(np.argmax(evals))]
    if float(np.linalg.norm(major)) < 1e-6:
        return None
    return _normalize_angle_deg(math.degrees(math.atan2(float(major[1]), float(major[0]))))


def _fuse_orientation_deg(
    *,
    pca_deg: float | None,
    rect_deg: float,
    edge_deg: float | None,
) -> tuple[float, str]:
    """Combina PCA + lato lungo; minAreaRect solo come fallback."""
    if pca_deg is None and edge_deg is None:
        return rect_deg, "min_area_rect"
    cand = pca_deg if pca_deg is not None else edge_deg
    method = "pca_mask" if pca_deg is not None else "long_edge"
    if edge_deg is not None and pca_deg is not None:
        if abs(_shortest_angle_delta_deg(edge_deg, pca_deg)) > 20.0:
            edge_flip = _normalize_angle_deg(edge_deg + 90.0)
            if abs(_shortest_angle_delta_deg(edge_flip, pca_deg)) < abs(
                _shortest_angle_delta_deg(edge_deg, pca_deg)
            ):
                edge_deg = edge_flip
        cand = _circular_mean_deg(pca_deg, edge_deg, wa=0.65, wb=0.35)
        method = "pca_long_edge"
    if abs(_shortest_angle_delta_deg(cand, rect_deg)) > 35.0:
        return cand, method
    return _circular_mean_deg(cand, rect_deg, wa=0.75, wb=0.25), method


def _orient_axis_from_center(cx: float, cy: float, orient_deg: float, length: float) -> list[list[float]]:
    import math

    rad = math.radians(orient_deg)
    return [
        [round(cx, 1), round(cy, 1)],
        [round(cx + length * math.cos(rad), 1), round(cy + length * math.sin(rad), 1)],
    ]


def _edge_angles_from_box_pts(box_pts: np.ndarray | list[Any]) -> list[tuple[float, float]]:
    """Ritorna [(lunghezza_lato, angolo_deg), ...] per i 4 lati del rettangolo."""
    import math

    pts = np.asarray(box_pts, dtype=np.float64).reshape(-1, 2)
    if pts.shape[0] < 4:
        return []
    edges: list[tuple[float, float]] = []
    for i in range(4):
        p0 = pts[i]
        p1 = pts[(i + 1) % 4]
        dx = float(p1[0] - p0[0])
        dy = float(p1[1] - p0[1])
        ln = math.hypot(dx, dy)
        if ln < 1.0:
            continue
        edges.append((ln, math.degrees(math.atan2(dy, dx))))
    return edges


def canonical_rectangle_axes(
    *,
    cx: float,
    cy: float,
    box_pts: np.ndarray | list[Any],
    rw: float | None = None,
    rh: float | None = None,
    long_deg_hint: float | None = None,
) -> dict[str, Any]:
    """
    Geometria fissa rettangolo:
    - orient_axis_px (magenta) = lato LUNGO
    - grip_align_axis_px (ciano) = lato CORTO (pinza chiude qui)
    """
    edges = _edge_angles_from_box_pts(box_pts)
    if not edges:
        long_len = max(float(rw or 40.0), float(rh or 40.0))
        short_len = min(float(rw or 40.0), float(rh or 40.0))
        long_ang = _normalize_angle_deg(float(long_deg_hint or 0.0))
        short_ang = _normalize_angle_deg(long_ang + 90.0)
    else:
        edges_sorted = sorted(edges, key=lambda e: -e[0])
        long_len = float(edges_sorted[0][0])
        short_len = float(edges_sorted[-1][0])
        if rw is not None and rh is not None and rw > 1.0 and rh > 1.0:
            long_len = max(float(rw), float(rh))
            short_len = min(float(rw), float(rh))
        long_ang = _normalize_angle_deg(edges_sorted[0][1])
        if long_deg_hint is not None:
            hint = _normalize_angle_deg(float(long_deg_hint))
            candidates = [
                long_ang,
                _normalize_angle_deg(long_ang + 90.0),
                _normalize_angle_deg(long_ang + 180.0),
                _normalize_angle_deg(long_ang - 90.0),
            ]
            long_ang = min(
                candidates,
                key=lambda a: abs(_shortest_angle_delta_deg(a, hint)),
            )
        short_ang = _normalize_angle_deg(long_ang + 90.0)
    half_long = max(long_len * 0.48, 12.0)
    half_short = max(short_len * 0.48, 10.0)
    return {
        "orientation_deg": round(long_ang, 2),
        "grip_align_deg": round(short_ang, 2),
        "orient_axis_px": _orient_axis_from_center(cx, cy, long_ang, half_long),
        "grip_align_axis_px": _orient_axis_from_center(cx, cy, short_ang, half_short),
        "grip_axis_px": _orient_axis_from_center(cx, cy, short_ang, half_short),
        "box_long_side_px": round(long_len, 1),
        "box_short_side_px": round(short_len, 1),
        "aspect_ratio": round(long_len / max(short_len, 1.0), 2),
        "axis_geometry": "long_short_fixed",
    }


def refresh_detection_rectangle_axes(det: dict[str, Any]) -> dict[str, Any]:
    """Ricalcola assi dal box orientato — l'ungo e il corto non si scambiano mai."""
    if not isinstance(det, dict) or not det.get("ok"):
        return det
    obox = det.get("orient_box_px")
    if not isinstance(obox, list) or len(obox) < 4:
        return det
    gcx, gcy = det.get("grip_center_px") or det.get("bbox_center_px") or [0, 0]
    rw = det.get("rect_rw")
    rh = det.get("rect_rh")
    if rw is None or rh is None:
        edges = _edge_angles_from_box_pts(obox)
        if edges:
            lens = sorted((e[0] for e in edges), reverse=True)
            rw = lens[0]
            rh = lens[-1]
    axes = canonical_rectangle_axes(
        cx=float(gcx),
        cy=float(gcy),
        box_pts=obox,
        rw=float(rw) if rw is not None else None,
        rh=float(rh) if rh is not None else None,
        long_deg_hint=det.get("orientation_deg"),
    )
    out = dict(det)
    out.update(axes)
    return out


def _color_box_hsv_mask(frame: np.ndarray) -> np.ndarray:
    """Maschera HSV per scatoletta blu (tunable via env)."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h_min = _parse_int_env("D1_COLOR_BOX_H_MIN", 95)
    h_max = _parse_int_env("D1_COLOR_BOX_H_MAX", 130)
    s_min = _parse_int_env("D1_COLOR_BOX_S_MIN", 45)
    v_min = _parse_int_env("D1_COLOR_BOX_V_MIN", 35)
    lower = np.array([max(0, h_min), max(0, s_min), max(0, v_min)], dtype=np.uint8)
    upper = np.array([min(179, h_max), 255, 255], dtype=np.uint8)
    mask = cv2.inRange(hsv, lower, upper)
    k = max(3, _parse_int_env("D1_COLOR_BOX_MORPH_K", 5))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    return mask


def _score_color_candidate(
    *,
    area: float,
    cx: float,
    cy: float,
    rw: float,
    rh: float,
    frame_hw: tuple[int, int],
) -> float:
    """Preferisce blob blu grande, centrato in X, nella metà inferiore (tavolo/pezzo)."""
    h, w = frame_hw
    frame_area = max(float(w * h), 1.0)
    area_ratio = area / frame_area
    x_pen = abs(cx - w / 2.0) / max(w / 2.0, 1.0)
    # Pezzo sul tavolo: penalizza bbox troppo in alto (YOLO spesso sbaglia verso l'alto).
    y_target = h * _parse_float_env("D1_COLOR_BOX_Y_TARGET_FRAC", 0.62)
    y_pen = abs(cy - y_target) / max(h * 0.5, 1.0)
    aspect = max(rw, rh) / max(min(rw, rh), 1.0)
    aspect_pen = 0.0 if 1.1 <= aspect <= 4.5 else 0.25
    return area_ratio - 0.18 * x_pen - 0.22 * y_pen - aspect_pen


def _detection_from_color_contour(
    frame: np.ndarray,
    cnt: np.ndarray,
    *,
    score: float,
    elapsed_s: float,
    mask: np.ndarray | None = None,
) -> dict[str, Any]:
    h, w = int(frame.shape[0]), int(frame.shape[1])
    (cx, cy), (rw, rh), angle = cv2.minAreaRect(cnt)
    cx_f, cy_f = float(cx), float(cy)
    rw_f, rh_f = float(rw), float(rh)
    rect_orient = _angle_from_min_area_rect(rw_f, rh_f, angle)
    box_pts = cv2.boxPoints(((cx, cy), (rw, rh), angle))
    pca_orient = _orientation_from_mask_pca(mask, cnt) if mask is not None else None
    edge_orient = _long_edge_angle_from_box_pts(box_pts)
    orient, orient_method = _fuse_orientation_deg(
        pca_deg=pca_orient,
        rect_deg=rect_orient,
        edge_deg=edge_orient,
    )
    x1 = float(np.min(box_pts[:, 0]))
    y1 = float(np.min(box_pts[:, 1]))
    x2 = float(np.max(box_pts[:, 0]))
    y2 = float(np.max(box_pts[:, 1]))
    bw = max(1.0, x2 - x1)
    bh = max(1.0, y2 - y1)
    base = {
        "ok": True,
        "backend": "color_blue_box",
        "label": "blue_box",
        "confidence": round(max(0.05, min(0.99, score + 0.35)), 4),
        "bbox_xyxy": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
        "bbox_center_px": [round(cx_f, 1), round(cy_f, 1)],
        "bbox_size_px": [round(bw, 1), round(bh, 1)],
        "bbox_area_px": round(bw * bh, 1),
        "bbox_area_ratio": round((bw * bh) / max(w * h, 1), 5),
        "norm": [
            round((cx_f - w / 2.0) / max(w / 2.0, 1.0), 4),
            round((cy_f - h / 2.0) / max(h / 2.0, 1.0), 4),
        ],
        "grip_center_px": [round(cx_f, 1), round(cy_f, 1)],
        "gripper_model": "color_box_center",
        "latency_ms": round(elapsed_s * 1000.0, 2),
        "all_count": 1,
        "orientation_deg_rect": rect_orient,
        "orientation_deg_pca": pca_orient,
        "orientation_deg_edge": edge_orient,
        "orient_method": orient_method,
        "orient_box_px": [[round(float(p[0]), 1), round(float(p[1]), 1)] for p in box_pts],
        "rect_rw": round(rw_f, 2),
        "rect_rh": round(rh_f, 2),
        "detect_method": "hsv_blue_contour",
    }
    base["orientation_deg"] = orient
    return refresh_detection_rectangle_axes(base)


def _detect_color_box(frame: np.ndarray) -> dict[str, Any]:
    """Rileva scatoletta blu per maschera HSV + minAreaRect (posizione e rotazione reali)."""
    t0 = time.perf_counter()
    h, w = frame.shape[:2]
    mask = _color_box_hsv_mask(frame)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return {
            "ok": False,
            "backend": "color_blue_box",
            "reason": "no_blue_contour",
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    min_area = _parse_float_env("D1_COLOR_BOX_MIN_AREA_FRAC", 0.012) * float(w * h)
    min_solidity = _parse_float_env("D1_COLOR_BOX_MIN_SOLIDITY", 0.55)
    best_cnt = None
    best_score = -1.0
    for cnt in cnts:
        area = float(cv2.contourArea(cnt))
        if area < min_area:
            continue
        hull = cv2.convexHull(cnt)
        hull_area = float(cv2.contourArea(hull))
        if hull_area < 1.0:
            continue
        solidity = area / hull_area
        if solidity < min_solidity:
            continue
        (cx, cy), (rw, rh), _angle = cv2.minAreaRect(cnt)
        if min(rw, rh) < 12.0:
            continue
        sc = _score_color_candidate(
            area=area,
            cx=float(cx),
            cy=float(cy),
            rw=float(rw),
            rh=float(rh),
            frame_hw=(h, w),
        )
        if sc > best_score:
            best_score = sc
            best_cnt = cnt
    if best_cnt is None:
        return {
            "ok": False,
            "backend": "color_blue_box",
            "reason": "blue_contour_filtered",
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    cnt_mask = np.zeros((h, w), dtype=np.uint8)
    cv2.drawContours(cnt_mask, [best_cnt], -1, 255, thickness=-1)
    local_mask = cv2.bitwise_and(mask, cnt_mask)
    return _detection_from_color_contour(
        frame,
        best_cnt,
        score=best_score,
        elapsed_s=time.perf_counter() - t0,
        mask=local_mask,
    )


def _orientation_deg_from_bbox_roi(frame: np.ndarray, xyxy: list[float]) -> float:
    """Stima rotazione del pezzo nel bbox (minAreaRect sul contorno)."""
    if frame is None or not getattr(frame, "size", 0) or len(xyxy) < 4:
        return 0.0
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = [int(v) for v in xyxy[:4]]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w - 1, x2), min(h - 1, y2)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return 0.0
    roi = frame[y1:y2, x1:x2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, thr = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cnts, _ = cv2.findContours(thr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return 0.0
    cnt = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(cnt) < 80.0:
        return 0.0
    _, (rw, rh), angle = cv2.minAreaRect(cnt)
    if rw < 1.0 or rh < 1.0:
        return 0.0
    if rw < rh:
        angle = float(angle) + 90.0
    return _normalize_angle_deg(angle)


def enrich_detection_orientation(frame: np.ndarray, det: dict[str, Any]) -> dict[str, Any]:
    """Aggiunge orientamento pezzo e asse per overlay / correzione J5."""
    if not det.get("ok"):
        return det
    xyxy = det.get("bbox_xyxy") or []
    orient = _orientation_deg_from_bbox_roi(frame, xyxy)
    cx, cy = det.get("bbox_center_px") or det.get("grip_center_px") or [0, 0]
    import math

    length = max(float(det.get("bbox_size_px", [40, 40])[0]), 30.0) * 0.42
    rad = math.radians(orient)
    ax2 = float(cx) + length * math.cos(rad)
    ay2 = float(cy) + length * math.sin(rad)
    out = dict(det)
    out["orientation_deg"] = orient
    out["orient_axis_px"] = [[round(float(cx), 1), round(float(cy), 1)], [round(ax2, 1), round(ay2, 1)]]
    return out


def _select_detection(
    boxes: list[dict[str, Any]],
    frame: np.ndarray,
    backend: str,
    elapsed_s: float,
) -> dict[str, Any]:
    h, w = int(frame.shape[0]), int(frame.shape[1])
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
    base = {
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
    return enrich_detection_orientation(frame, base)


def _bbox_area(xyxy: list[float]) -> float:
    if len(xyxy) < 4:
        return 0.0
    return max(0.0, float(xyxy[2]) - float(xyxy[0])) * max(0.0, float(xyxy[3]) - float(xyxy[1]))


def detect_all_objects(frame: np.ndarray) -> dict[str, Any]:
    """Tutte le detection YOLO (o una sola proposta classic) — per vision dinamica."""
    model_path = os.environ.get("GO2_YOLO_MODEL", "").strip()
    t0 = time.perf_counter()
    if model_path:
        p = Path(model_path).expanduser()
        if p.is_file():
            try:
                model = _load_ultralytics_model(str(p))
                imgsz = int(os.environ.get("GO2_YOLO_IMGSZ", "640"))
                conf_min = float(os.environ.get("GO2_YOLO_CONF", "0.25"))
                results = model.predict(frame, imgsz=imgsz, conf=conf_min, verbose=False)
                boxes: list[dict[str, Any]] = []
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
                        boxes.append(
                            {
                                "bbox_xyxy": xyxy,
                                "confidence": round(conf, 4),
                                "class_id": cls_i,
                                "label": label,
                            }
                        )
                return {
                    "ok": bool(boxes),
                    "backend": "ultralytics",
                    "model_path": str(p),
                    "boxes": boxes,
                    "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                }
            except Exception as exc:
                err = repr(exc)
                # yolo11.pt su ultralytics vecchio (Jetson): riprova yolov8n se presente
                if "C3k2" in err or "yolo11" in str(p).lower():
                    v8 = p.parent / "yolov8n.pt"
                    if v8.is_file() and v8 != p:
                        try:
                            model = _load_ultralytics_model(str(v8))
                            results = model.predict(
                                frame,
                                imgsz=int(os.environ.get("GO2_YOLO_IMGSZ", "640")),
                                conf=float(os.environ.get("GO2_YOLO_CONF", "0.20")),
                                verbose=False,
                            )
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
                                    boxes.append(
                                        {
                                            "bbox_xyxy": xyxy,
                                            "confidence": round(conf, 4),
                                            "class_id": cls_i,
                                            "label": label,
                                        }
                                    )
                            return {
                                "ok": bool(boxes),
                                "backend": "ultralytics",
                                "model_path": str(v8),
                                "boxes": boxes,
                                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                            }
                        except Exception:
                            pass
                return {
                    "ok": False,
                    "backend": "ultralytics",
                    "reason": err,
                    "boxes": [],
                    "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                }
    if os.environ.get("GO2_CLASSIC_BOX_FALLBACK", "1").lower() in {"1", "true", "yes"}:
        one = _classic_box_proposal(frame)
        boxes = []
        if one.get("ok"):
            boxes.append(
                {
                    "bbox_xyxy": one["bbox_xyxy"],
                    "confidence": one.get("confidence", 0.0),
                    "class_id": -1,
                    "label": one.get("label", "box_proposal"),
                }
            )
        return {
            "ok": bool(boxes),
            "backend": one.get("backend", "classic_contour_fallback"),
            "boxes": boxes,
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    return {
        "ok": False,
        "backend": "disabled",
        "reason": "GO2_YOLO_MODEL mancante",
        "boxes": [],
        "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
    }


def _color_only_pick_detect() -> bool:
    """Dashboard presa D1: solo scatoletta blu HSV — mai YOLO/classic."""
    backend = _pick_detect_backend()
    if backend in {"yolo", "ultralytics", "classic", "classic_contour"}:
        return False
    if os.environ.get("D1_PICK_COLOR_ONLY", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    return True


def detect_box_object(frame: np.ndarray) -> dict[str, Any]:
    """Rilevamento pezzo per presa D1 — default: color_blue_box + orientamento minAreaRect."""
    if _color_only_pick_detect():
        return _detect_color_box(frame)

    backend = _pick_detect_backend()
    if backend in {"color", "blue", "color_blue", "color_blue_box", "color_then_yolo"}:
        out = _detect_color_box(frame)
        if out.get("ok") or backend != "color_then_yolo":
            return out

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

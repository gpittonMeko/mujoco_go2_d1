#!/usr/bin/env python3
"""
Optional box detector for the Go2+D1 grasp dashboard.

The hot path prefers an exported Ultralytics model (TensorRT .engine, ONNX, or
.pt if explicitly configured). If no model is present, a conservative OpenCV
proposal can provide a bbox for lab bring-up; the UI reports that fallback
clearly so it is not confused with a trained detector.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

_MODEL_CACHE: dict[str, Any] = {}
_MODEL_META_CACHE: dict[str, dict[str, Any]] = {}

# Calibrazione colore persistita (data/color_box_calib.json): le soglie HSV della scatola
# si risolvono con priorita' env > file di calibrazione > default. Permette di tarare il
# colore dal vivo (auto-calibrazione dal polso) senza riavviare la dashboard.
_COLOR_CALIB_CACHE: dict[str, Any] = {"mtime": -1.0, "data": {}}
_COLOR_PROFILES_CACHE: dict[str, Any] = {"mtime": -1.0, "data": {}}
# Se True, _color_int/_color_float ignorano data/color_box_calib.json (fallback detection).
_IGNORE_COLOR_CALIB = False


def _profiles_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "color_profiles.json"


def _load_color_profiles_doc() -> dict[str, Any]:
    p = _profiles_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    if mtime != _COLOR_PROFILES_CACHE["mtime"]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        _COLOR_PROFILES_CACHE["data"] = data if isinstance(data, dict) else {}
        _COLOR_PROFILES_CACHE["mtime"] = mtime
    return _COLOR_PROFILES_CACHE["data"]


def parse_color_from_instruction(text: str) -> str | None:
    """Estrae profilo colore da frase operatore (blu / rosso / verde / grigio)."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if re.search(r"\b(blu|blue|azzurr\w*)\b", t):
        return "blu"
    if re.search(r"\b(ross[oae]|red)\b", t):
        return "rosso"
    if re.search(r"\b(verd[eiae]|green)\b", t):
        return "verde"
    if re.search(r"\b(grigi[oaie]?|grey|gray|cilindr\w*)\b", t):
        return "grigio"
    return None


def _resolve_color_profile(color_hint: str | None) -> dict[str, Any] | None:
    if not color_hint:
        return None
    profiles = (_load_color_profiles_doc().get("profiles") or {})
    key = str(color_hint).strip().lower()
    if key in profiles and isinstance(profiles[key], dict):
        return dict(profiles[key])
    aliases = {"blue": "blu", "red": "rosso", "green": "verde", "gray": "grigio", "grey": "grigio"}
    alias = aliases.get(key)
    if alias and alias in profiles and isinstance(profiles[alias], dict):
        return dict(profiles[alias])
    return None


def _calib_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "color_box_calib.json"


def _load_color_calib() -> dict[str, Any]:
    if _IGNORE_COLOR_CALIB:
        return {}
    p = _calib_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    if mtime != _COLOR_CALIB_CACHE["mtime"]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        _COLOR_CALIB_CACHE["data"] = data if isinstance(data, dict) else {}
        _COLOR_CALIB_CACHE["mtime"] = mtime
    return _COLOR_CALIB_CACHE["data"]


def _color_int(name: str, default: int) -> int:
    """Soglia colore intera: env > file calibrazione > default."""
    raw = (os.environ.get(name) or "").strip()
    if raw:
        try:
            return int(float(raw))
        except ValueError:
            pass
    val = _load_color_calib().get(name)
    if val is not None:
        try:
            return int(float(val))
        except (ValueError, TypeError):
            pass
    return default


def _color_float(name: str, default: float) -> float:
    """Soglia colore float: env > file calibrazione > default."""
    raw = (os.environ.get(name) or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    val = _load_color_calib().get(name)
    if val is not None:
        try:
            return float(val)
        except (ValueError, TypeError):
            pass
    return default


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
            "h_min": _color_int("D1_COLOR_BOX_H_MIN", 95),
            "h_max": _color_int("D1_COLOR_BOX_H_MAX", 130),
            "s_min": _color_int("D1_COLOR_BOX_S_MIN", 45),
            "v_min": _color_int("D1_COLOR_BOX_V_MIN", 35),
            "calibrated": bool(_load_color_calib()),
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


def _orient_axis_from_center(cx: float, cy: float, orient_deg: float, length: float) -> list[list[float]]:
    import math

    rad = math.radians(orient_deg)
    return [
        [round(cx, 1), round(cy, 1)],
        [round(cx + length * math.cos(rad), 1), round(cy + length * math.sin(rad), 1)],
    ]


def _apply_gripper_exclude_crop(mask: np.ndarray) -> np.ndarray:
    """Azzera la fascia basso-centrale dove compaiono le chele/pinza nel frame polso.

    Le chele non sono blu ma il minAreaRect di blob vicini puo' inglobarle; escludendo
    fisicamente quella ROI dalla maschera si evita che contorni o bbox scendano sulla pinza.
    """
    h, w = int(mask.shape[0]), int(mask.shape[1])
    bottom_frac = min(max(_parse_float_env("D1_PICK_GRIPPER_EXCLUDE_BOTTOM_FRAC", 0.20), 0.0), 0.45)
    width_frac = min(max(_parse_float_env("D1_PICK_GRIPPER_EXCLUDE_WIDTH_FRAC", 0.62), 0.2), 1.0)
    if bottom_frac <= 0.0:
        return mask
    y0 = int(round(h * (1.0 - bottom_frac)))
    if y0 < 0 or y0 >= h:
        return mask
    x_margin = int(round(w * (1.0 - width_frac) / 2.0))
    x0 = max(0, x_margin)
    x1 = min(w, w - x_margin)
    if x1 > x0:
        mask[y0:, x0:x1] = 0
    return mask


def _apply_detect_roi_crop(mask: np.ndarray) -> np.ndarray:
    """Azzera le fasce alta/bassa della maschera per escludere zone non-oggetto.

    Sul polso (Orbbec) la **fascia bassa** dell'inquadratura e' occupata dal corpo del
    cane e dalle chele: senza questo crop il detector li preferisce alla scatola. Tunable:
      * ``D1_PICK_BOTTOM_CROP_FRAC`` (default 0.30) — frazione bassa azzerata (cane+chele);
      * ``D1_PICK_TOP_CROP_FRAC``    (default 0.0)  — frazione alta azzerata (orizzonte);
      * ``D1_PICK_GRIPPER_EXCLUDE_*`` — ulteriore ROI centrale pinza sopra il crop globale.
    """
    h = int(mask.shape[0])
    bottom_frac = min(max(_parse_float_env("D1_PICK_BOTTOM_CROP_FRAC", 0.30), 0.0), 0.6)
    top_frac = min(max(_parse_float_env("D1_PICK_TOP_CROP_FRAC", 0.0), 0.0), 0.6)
    if bottom_frac > 0.0:
        y0 = int(round(h * (1.0 - bottom_frac)))
        if 0 <= y0 < h:
            mask[y0:, :] = 0
    if top_frac > 0.0:
        y1 = int(round(h * top_frac))
        if 0 < y1 <= h:
            mask[:y1, :] = 0
    return _apply_gripper_exclude_crop(mask)


def _color_box_hsv_mask(frame: np.ndarray, profile: dict[str, Any] | None = None) -> np.ndarray:
    """Maschera HSV per scatoletta colorata (tunable via env o color_profiles.json)."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    if profile:
        s_min = int(profile.get("s_min", 45))
        s_max = int(profile.get("s_max", 255))
        v_min = int(profile.get("v_min", 35))
        v_max = int(profile.get("v_max", 255))
        if profile.get("h_any"):
            lower = np.array([0, max(0, s_min), max(0, v_min)], dtype=np.uint8)
            upper = np.array([179, min(255, s_max), min(255, v_max)], dtype=np.uint8)
            mask = cv2.inRange(hsv, lower, upper)
        elif profile.get("h_wrap"):
            mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
            for hr in profile.get("h_ranges") or []:
                if isinstance(hr, (list, tuple)) and len(hr) >= 2:
                    h0, h1 = int(hr[0]), int(hr[1])
                    lower = np.array([max(0, h0), max(0, s_min), max(0, v_min)], dtype=np.uint8)
                    upper = np.array([min(179, h1), min(255, s_max), min(255, v_max)], dtype=np.uint8)
                    mask = cv2.bitwise_or(mask, cv2.inRange(hsv, lower, upper))
        else:
            h_min = int(profile.get("h_min", 95))
            h_max = int(profile.get("h_max", 130))
            lower = np.array([max(0, h_min), max(0, s_min), max(0, v_min)], dtype=np.uint8)
            upper = np.array([min(179, h_max), min(255, s_max), min(255, v_max)], dtype=np.uint8)
            mask = cv2.inRange(hsv, lower, upper)
        h, w = mask.shape[:2]
        x_min_frac = profile.get("x_min_frac")
        x_max_frac = profile.get("x_max_frac")
        y_min_frac = profile.get("y_min_frac")
        y_max_frac = profile.get("y_max_frac")
        if x_min_frac is not None:
            mask[:, : int(w * float(x_min_frac))] = 0
        if x_max_frac is not None:
            mask[:, int(w * float(x_max_frac)) :] = 0
        if y_min_frac is not None:
            mask[: int(h * float(y_min_frac)), :] = 0
        if y_max_frac is not None:
            mask[int(h * float(y_max_frac)) :, :] = 0
    else:
        h_min = _color_int("D1_COLOR_BOX_H_MIN", 95)
        h_max = _color_int("D1_COLOR_BOX_H_MAX", 130)
        s_min = _color_int("D1_COLOR_BOX_S_MIN", 45)
        v_min = _color_int("D1_COLOR_BOX_V_MIN", 35)
        lower = np.array([max(0, h_min), max(0, s_min), max(0, v_min)], dtype=np.uint8)
        upper = np.array([min(179, h_max), 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower, upper)
    k = max(3, _parse_int_env("D1_COLOR_BOX_MORPH_K", 5))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = _apply_detect_roi_crop(mask)
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
    # Con la fascia bassa (corpo del cane) gia' esclusa dal crop, NON spingiamo piu' la
    # scelta verso il basso: bias y neutro (centro frame) e peso ridotto, cosi' la scatola
    # nella zona alta/centrale non viene penalizzata a favore di blob piu' bassi.
    y_target = h * _parse_float_env("D1_COLOR_BOX_Y_TARGET_FRAC", 0.5)
    y_pen = abs(cy - y_target) / max(h * 0.5, 1.0)
    y_pen_w = _parse_float_env("D1_COLOR_BOX_Y_PEN_W", 0.10)
    aspect = max(rw, rh) / max(min(rw, rh), 1.0)
    aspect_pen = 0.0 if 1.1 <= aspect <= 4.5 else 0.25
    return area_ratio - 0.18 * x_pen - y_pen_w * y_pen - aspect_pen


def _detection_confidence(
    *,
    cnt_area: float,
    bbox_area: float,
    solidity: float,
    frame_area: float,
    area_ratio: float,
) -> float:
    """Confidenza euristica per color_blue_box: compattezza + dimensione da oggetto."""
    extent = cnt_area / max(bbox_area, 1.0)
    target = _parse_float_env("D1_COLOR_BOX_TARGET_AREA_RATIO", 0.018)
    if area_ratio <= target:
        size_fit = max(0.35, area_ratio / max(target, 1e-6))
    else:
        size_fit = max(0.25, target / area_ratio)
    # Penalizza blob enormi (pavimento/riflessi) che prima passavano con conf alta ma bbox gigante.
    if area_ratio > _parse_float_env("D1_COLOR_BOX_MAX_AREA_RATIO", 0.10):
        size_fit *= 0.35
    raw = 0.30 * min(1.0, solidity) + 0.28 * min(1.0, extent) + 0.42 * min(1.0, size_fit)
    return round(max(0.15, min(0.95, raw)), 4)


def _detection_from_color_contour(
    frame: np.ndarray,
    cnt: np.ndarray,
    *,
    score: float,
    elapsed_s: float,
    solidity: float = 0.0,
    label: str = "blue_box",
    backend: str = "color_blue_box",
) -> dict[str, Any]:
    h, w = int(frame.shape[0]), int(frame.shape[1])
    cnt_area = float(cv2.contourArea(cnt))
    (cx, cy), (rw, rh), angle = cv2.minAreaRect(cnt)
    cx_f, cy_f = float(cx), float(cy)
    rw_f, rh_f = float(rw), float(rh)
    orient = _angle_from_min_area_rect(rw_f, rh_f, angle)
    box_pts = cv2.boxPoints(((cx, cy), (rw, rh), angle))
    mar_x1 = float(np.min(box_pts[:, 0]))
    mar_y1 = float(np.min(box_pts[:, 1]))
    mar_x2 = float(np.max(box_pts[:, 0]))
    mar_y2 = float(np.max(box_pts[:, 1]))
    # Bbox metrica: preferisci il rettangolo stretto del contorno (evita che minAreaRect
    # allarghi verso le chele quando il blob e' inclinato o rumoroso).
    bx, by, bw, bh = cv2.boundingRect(cnt)
    x1, y1, x2, y2 = float(bx), float(by), float(bx + bw), float(by + bh)
    mar_area = max(1.0, (mar_x2 - mar_x1) * (mar_y2 - mar_y1))
    tight_area = max(1.0, bw * bh)
    if mar_area < tight_area * 1.8:
        x1, y1, x2, y2 = mar_x1, mar_y1, mar_x2, mar_y2
    bw = max(1.0, x2 - x1)
    bh = max(1.0, y2 - y1)
    bbox_area = bw * bh
    area_ratio = bbox_area / max(w * h, 1)
    if solidity <= 0.0 and cnt_area > 0:
        hull = cv2.convexHull(cnt)
        solidity = cnt_area / max(float(cv2.contourArea(hull)), 1.0)
    conf = _detection_confidence(
        cnt_area=cnt_area,
        bbox_area=bbox_area,
        solidity=float(solidity),
        frame_area=float(w * h),
        area_ratio=float(area_ratio),
    )
    long_side = max(rw_f, rh_f)
    base = {
        "ok": True,
        "backend": backend,
        "label": label,
        "confidence": conf,
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
        "grip_axis_px": _orient_axis_from_center(cx_f, cy_f, orient, long_side * 0.45),
        "gripper_model": "color_box_center",
        "latency_ms": round(elapsed_s * 1000.0, 2),
        "all_count": 1,
        "orientation_deg": orient,
        "orient_axis_px": _orient_axis_from_center(cx_f, cy_f, orient, long_side * 0.45),
        "orient_box_px": [[round(float(p[0]), 1), round(float(p[1]), 1)] for p in box_pts],
        "contour_area_px": round(cnt_area, 1),
        "solidity": round(float(solidity), 3),
        "extent": round(cnt_area / max(bbox_area, 1.0), 3),
        "detect_method": "hsv_blue_contour",
    }
    return base


def _detect_color_box_core(frame: np.ndarray, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rileva scatoletta colorata per maschera HSV + minAreaRect."""
    t0 = time.perf_counter()
    h, w = frame.shape[:2]
    label = str((profile or {}).get("label") or "blue_box")
    backend = "color_box_" + label.replace("_box", "")
    mask = _color_box_hsv_mask(frame, profile=profile)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return {
            "ok": False,
            "backend": "color_blue_box",
            "reason": "no_blue_contour",
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    min_area = _color_float("D1_COLOR_BOX_MIN_AREA_FRAC", 0.004) * float(w * h)
    max_area = _color_float("D1_COLOR_BOX_MAX_AREA_FRAC", 0.10) * float(w * h)
    min_solidity = _color_float("D1_COLOR_BOX_MIN_SOLIDITY", 0.55)
    best_cnt = None
    best_score = -1.0
    best_solidity = 0.0
    max_cy_frac = _parse_float_env("D1_COLOR_BOX_MAX_CY_FRAC", 0.68)
    max_bbox_h_frac = float((profile or {}).get("max_bbox_h_frac", _parse_float_env("D1_COLOR_BOX_MAX_BBOX_H_FRAC", 0.34)))
    for cnt in cnts:
        area = float(cv2.contourArea(cnt))
        if area < min_area or area > max_area:
            continue
        hull = cv2.convexHull(cnt)
        hull_area = float(cv2.contourArea(hull))
        if hull_area < 1.0:
            continue
        solidity = area / hull_area
        if solidity < min_solidity:
            continue
        bx, by, bw, bh = cv2.boundingRect(cnt)
        if bh > h * max_bbox_h_frac:
            continue
        if (by + bh) > h * _parse_float_env("D1_COLOR_BOX_MAX_BOTTOM_Y_FRAC", 0.72):
            continue
        (cx, cy), (rw, rh), _angle = cv2.minAreaRect(cnt)
        if min(rw, rh) < 8.0:
            continue
        if float(cy) > h * max_cy_frac:
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
            best_solidity = solidity
    if best_cnt is None:
        return {
            "ok": False,
            "backend": "color_blue_box",
            "reason": "blue_contour_filtered",
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    return _detection_from_color_contour(
        frame,
        best_cnt,
        score=best_score,
        elapsed_s=time.perf_counter() - t0,
        solidity=best_solidity,
        label=label,
        backend=backend,
    )


def _detect_color_box(frame: np.ndarray, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Detection HSV con fallback se la calibrazione salvata è troppo stretta per la luce attuale."""
    det = _detect_color_box_core(frame, profile=profile)
    if det.get("ok"):
        return det
    cal = _load_color_calib()
    if not cal:
        return det
    reason = str(det.get("reason") or "")
    if reason not in ("no_blue_contour", "blue_contour_filtered"):
        return det
    try:
        s_cal = int(float(cal.get("D1_COLOR_BOX_S_MIN", 0)))
    except (TypeError, ValueError):
        s_cal = 0
    if s_cal < 80:
        return det
    global _IGNORE_COLOR_CALIB
    _IGNORE_COLOR_CALIB = True
    try:
        fb = _detect_color_box_core(frame)
    finally:
        _IGNORE_COLOR_CALIB = False
    if not fb.get("ok"):
        return det
    out = dict(fb)
    out["calib_fallback"] = True
    out["reason_prev"] = reason
    out["hint_it"] = (
        "Calibrazione colore troppo stretta per questa luce — usate soglie default. "
        "Ricalibra dal vivo (Calibra colore) con la scatola inquadrata."
    )
    return out


def calibrate_color_from_frame(
    frame: np.ndarray,
    *,
    bbox_norm: list[float] | None = None,
    point_norm: list[float] | None = None,
    radius_frac: float = 0.06,
    margin_h: int = 12,
    margin_sv: int = 35,
) -> dict[str, Any]:
    """Campiona l'HSV della scatola e salva le soglie in ``data/color_box_calib.json``.

    Regione campionata (in ordine di priorita'):
      * ``bbox_norm`` = [x0,y0,x1,y1] normalizzati 0..1;
      * ``point_norm`` = [u,v] normalizzati 0..1 (+ ``radius_frac``);
      * **auto**: blob piu' saturo/compatto nella zona non-cropped (esclude la fascia bassa = cane).

    Applica le nuove soglie **subito** in-process (``os.environ``) e le **persiste** su file,
    cosi' valgono anche dopo un riavvio e per gli altri processi (jog 5053).
    """
    if frame is None or not getattr(frame, "size", 0):
        return {"ok": False, "reason": "no_frame"}
    h, w = int(frame.shape[0]), int(frame.shape[1])
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    region_kind = "auto"
    if bbox_norm and len(bbox_norm) >= 4:
        x0 = int(max(0, min(w - 1, round(float(bbox_norm[0]) * w))))
        y0 = int(max(0, min(h - 1, round(float(bbox_norm[1]) * h))))
        x1 = int(max(x0 + 1, min(w, round(float(bbox_norm[2]) * w))))
        y1 = int(max(y0 + 1, min(h, round(float(bbox_norm[3]) * h))))
        region_kind = "bbox"
        roi = hsv[y0:y1, x0:x1].reshape(-1, 3)
    elif point_norm and len(point_norm) >= 2:
        cx = int(max(0, min(w - 1, round(float(point_norm[0]) * w))))
        cy = int(max(0, min(h - 1, round(float(point_norm[1]) * h))))
        r = max(4, int(round(float(radius_frac) * min(w, h))))
        x0, y0, x1, y1 = max(0, cx - r), max(0, cy - r), min(w, cx + r), min(h, cy + r)
        region_kind = "point"
        roi = hsv[y0:y1, x0:x1].reshape(-1, 3)
    else:
        # AUTO robusto: localizza la scatola con una maschera blu PERMISSIVA (unico oggetto
        # blu in scena; il cane e' gia' fuori dal crop), poi campiona SOLO i pixel del blob
        # (dentro il contorno) — niente pavimento → tinta pulita ovunque sia la scatola.
        boot_lo = np.array([_color_int("D1_COLOR_BOOT_H_MIN", 90),
                            _color_int("D1_COLOR_BOOT_S_MIN", 40),
                            _color_int("D1_COLOR_BOOT_V_MIN", 40)], dtype=np.uint8)
        boot_hi = np.array([_color_int("D1_COLOR_BOOT_H_MAX", 135), 255, 255], dtype=np.uint8)
        boot = cv2.inRange(hsv, boot_lo, boot_hi)
        boot = _apply_detect_roi_crop(boot)
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        boot = cv2.morphologyEx(boot, cv2.MORPH_OPEN, k, iterations=1)
        boot = cv2.morphologyEx(boot, cv2.MORPH_CLOSE, k, iterations=2)
        cnts, _ = cv2.findContours(boot, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            return {"ok": False, "reason": "no_blue_object",
                    "hint_it": "Scatola blu non trovata nel frame del polso: portala in vista, "
                               "oppure indica point_norm/bbox_norm."}
        big = max(cnts, key=cv2.contourArea)
        if float(cv2.contourArea(big)) < 0.0012 * float(w * h):
            return {"ok": False, "reason": "blue_object_too_small",
                    "hint_it": "Oggetto blu troppo piccolo/lontano: avvicina la scatola o indica point_norm."}
        mblob = np.zeros((h, w), dtype=np.uint8)
        cv2.drawContours(mblob, [big], -1, 255, -1)
        ys, xs = np.where(mblob > 0)
        roi = hsv[ys, xs]
        x0, y0, bw, bh = cv2.boundingRect(big)
        x1, y1 = x0 + bw, y0 + bh
        region_kind = "auto_blue"

    if roi.shape[0] < 30:
        return {"ok": False, "reason": "roi_too_small", "region_px": [x0, y0, x1, y1]}

    # Scarta i pixel a bassa saturazione (bordi/ombre) prima di stimare la tinta.
    s_all = roi[:, 1]
    keep = roi[s_all >= max(20, int(np.percentile(s_all, 30)))]
    if keep.shape[0] < 30:
        keep = roi
    Hh = keep[:, 0].astype(np.int32)
    Ss = keep[:, 1].astype(np.int32)
    Vv = keep[:, 2].astype(np.int32)
    h_lo, h_hi = int(np.percentile(Hh, 5)), int(np.percentile(Hh, 95))
    s_lo, v_lo = int(np.percentile(Ss, 5)), int(np.percentile(Vv, 5))
    # Guard: una tinta troppo larga = la regione campionata NON e' un colore singolo
    # (probabile pavimento/sfondo dentro la ROI) → calibrazione inaffidabile, meglio rifiutare.
    if (h_hi - h_lo) > 45:
        return {
            "ok": False,
            "reason": "hue_too_broad",
            "hue_p5_p95": [h_lo, h_hi],
            "region_px": [x0, y0, x1, y1],
            "hint_it": "Regione non a tinta unica (pavimento incluso): indica un punto sul "
                       "colore pieno della scatola (point_norm) o un riquadro piu' stretto.",
        }
    bounds = {
        "D1_COLOR_BOX_H_MIN": int(max(0, h_lo - margin_h)),
        "D1_COLOR_BOX_H_MAX": int(min(179, h_hi + margin_h)),
        # Saturazione: non copiare s_p5 quasi verbatim — sotto luce diversa la scatola
        # desatura e la detection fallisce (no_blue_contour). Margine ampio su S, V permissivo.
        "D1_COLOR_BOX_S_MIN": int(max(40, min(s_lo - 10, s_lo - 70, int(s_lo * 0.58)))),
        "D1_COLOR_BOX_V_MIN": int(max(25, min(v_lo - 45, v_lo - 25))),
    }
    for kk, vv in bounds.items():
        os.environ[kk] = str(vv)
    payload = {
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "region_kind": region_kind,
        "region_px": [x0, y0, x1, y1],
        "hue_p5_p95": [h_lo, h_hi],
        "s_p5": s_lo,
        "v_p5": v_lo,
        "samples": int(keep.shape[0]),
        **bounds,
    }
    try:
        p = _calib_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _COLOR_CALIB_CACHE["mtime"] = -1.0  # forza rilettura
    except OSError as exc:
        return {"ok": False, "reason": "calib_write_failed", "detail": repr(exc), **payload}
    return {"ok": True, "calibration": payload, "applied": bounds, "frame": [w, h]}


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


def detect_box_object(frame: np.ndarray, *, color_hint: str | None = None) -> dict[str, Any]:
    """Rilevamento pezzo per presa D1 — default: color box HSV + orientamento minAreaRect."""
    profile = _resolve_color_profile(color_hint)
    if _color_only_pick_detect():
        out = _detect_color_box(frame, profile=profile)
        if color_hint and isinstance(out, dict):
            out["color_hint"] = color_hint
        return out

    backend = _pick_detect_backend()
    if backend in {"color", "blue", "color_blue", "color_blue_box", "color_then_yolo"}:
        out = _detect_color_box(frame, profile=profile)
        if color_hint and isinstance(out, dict):
            out["color_hint"] = color_hint
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

"""Salva frame + bbox detection per debug presa (``data/grasp_debug_*`` sulla NX)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT


def _save_debug_enabled() -> bool:
    return os.environ.get("GO2_GRASP_SAVE_DETECT_DEBUG", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _data_dir() -> Path:
    d = PROJECT_ROOT / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_detection_snapshot(
    frame_bgr: Any,
    detection: dict[str, Any] | None,
    *,
    tag: str,
    logical_camera: int,
    step: str,
) -> dict[str, Any]:
    """Scrive ``grasp_debug_{tag}.jpg`` + ``grasp_debug_{tag}.json`` e aggiorna ``grasp_debug_manifest.json``."""
    meta: dict[str, Any] = {
        "saved": False,
        "tag": tag,
        "logical_camera": int(logical_camera),
        "step": step,
    }
    if not _save_debug_enabled():
        meta["reason"] = "GO2_GRASP_SAVE_DETECT_DEBUG_off"
        return meta

    try:
        import cv2
    except Exception as exc:
        meta["reason"] = f"cv2_unavailable:{exc!r}"
        return meta

    det = detection if isinstance(detection, dict) else {}
    out_img = frame_bgr.copy()
    h, w = out_img.shape[:2]
    bbox = det.get("bbox_xyxy") or []
    ok = bool(det.get("ok"))
    color = (0, 220, 0) if ok else (0, 80, 255)
    if len(bbox) >= 4:
        x1, y1, x2, y2 = [int(round(float(v))) for v in bbox[:4]]
        cv2.rectangle(out_img, (x1, y1), (x2, y2), color, 2)
        cx = det.get("bbox_center_px") or [(x1 + x2) / 2, (y1 + y2) / 2]
        if isinstance(cx, (list, tuple)) and len(cx) >= 2:
            cv2.drawMarker(
                out_img,
                (int(round(float(cx[0]))), int(round(float(cx[1])))),
                (255, 200, 0),
                cv2.MARKER_CROSS,
                14,
                2,
            )
    # Asse di presa REALE del pezzo (minAreaRect) — giallo, con angolo in gradi.
    orient_axis = det.get("orient_axis_px") or det.get("grip_axis_px")
    if isinstance(orient_axis, (list, tuple)) and len(orient_axis) >= 2:
        try:
            p0 = (int(round(float(orient_axis[0][0]))), int(round(float(orient_axis[0][1]))))
            p1 = (int(round(float(orient_axis[1][0]))), int(round(float(orient_axis[1][1]))))
            cv2.line(out_img, p0, p1, (0, 215, 255), 2, cv2.LINE_AA)
        except (TypeError, IndexError, ValueError):
            pass
    # Fascia bassa esclusa (corpo del cane): linea di taglio indicativa.
    try:
        bottom_frac = float(os.environ.get("D1_PICK_BOTTOM_CROP_FRAC", "0.22") or "0.22")
        if 0.0 < bottom_frac < 0.6:
            yb = int(round(h * (1.0 - bottom_frac)))
            cv2.line(out_img, (0, yb), (w, yb), (80, 80, 80), 1, cv2.LINE_AA)
            cv2.putText(out_img, "crop cane", (8, min(h - 6, yb + 16)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (120, 120, 120), 1, cv2.LINE_AA)
    except (TypeError, ValueError):
        pass
    odeg = det.get("orientation_deg")
    label = f"cam{logical_camera} {det.get('backend', '?')} ok={ok} conf={det.get('confidence', '?')}"
    if odeg is not None:
        label += f" ang={odeg}"
    cv2.putText(out_img, label[:80], (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)

    dd = _data_dir()
    jpg_path = dd / f"grasp_debug_{tag}.jpg"
    json_path = dd / f"grasp_debug_{tag}.json"
    cv2.imwrite(str(jpg_path), out_img, [int(cv2.IMWRITE_JPEG_QUALITY), 88])

    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tag": tag,
        "step": step,
        "logical_camera": int(logical_camera),
        "frame_size_px": [w, h],
        "detection_ok": ok,
        "detection": det,
        "image_path": str(jpg_path),
        "image_url": f"/api/grasp/detection_debug/{tag}.jpg",
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    manifest_path = dd / "grasp_debug_manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    if not isinstance(manifest.get("snapshots"), dict):
        manifest["snapshots"] = {}
    manifest["updated_at"] = payload["saved_at"]
    manifest["snapshots"][tag] = {
        "logical_camera": logical_camera,
        "detection_ok": ok,
        "backend": det.get("backend"),
        "confidence": det.get("confidence"),
        "bbox_xyxy": det.get("bbox_xyxy"),
        "label": det.get("label"),
        "reason": det.get("reason"),
        "image_url": payload["image_url"],
        "json_path": str(json_path),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    meta.update(
        {
            "saved": True,
            "detection_ok": ok,
            "image_path": str(jpg_path),
            "image_url": payload["image_url"],
            "json_path": str(json_path),
            "manifest_path": str(manifest_path),
            "bbox_xyxy": det.get("bbox_xyxy"),
            "backend": det.get("backend"),
            "confidence": det.get("confidence"),
        }
    )
    return meta


def read_debug_manifest() -> dict[str, Any]:
    p = _data_dir() / "grasp_debug_manifest.json"
    if not p.is_file():
        return {"ok": False, "reason": "no_manifest", "hint_it": "Esegui run_full o box_detect prima."}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "reason": "manifest_read_failed", "detail": repr(exc)}
    return {"ok": True, **data}

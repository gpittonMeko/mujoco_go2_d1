"""Riconoscimento oggetto su snapshot camera polso (RealSense D456 o Orbbec legacy)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from go2_dashboard.d1_jog import orbbec_capture, pick_preset, pick_vision_crop
from go2_dashboard.paths import PROJECT_ROOT

_OVERLAY_NAME = "scene.jpg"


def _overlay_path() -> Path:
    return orbbec_capture._SNAP_DIR / _OVERLAY_NAME


def scene_overlay_path() -> Path:
    return _overlay_path()


def _read_bgr_from_jpeg(path: Path) -> Any:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    data = path.read_bytes()
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _draw_detection(frame: Any, det: dict[str, Any]) -> Any:
    import cv2
    import numpy as np

    out = frame.copy()
    if not det.get("ok"):
        cv2.putText(
            out,
            "Nessun oggetto",
            (12, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 80, 255),
            2,
            cv2.LINE_AA,
        )
        return out
    obox = det.get("orient_box_px")
    if isinstance(obox, list) and len(obox) >= 4:
        pts = np.array([[int(p[0]), int(p[1])] for p in obox[:4]], dtype=np.int32)
        cv2.polylines(out, [pts], isClosed=True, color=(0, 220, 255), thickness=2)
    else:
        xyxy = det.get("bbox_xyxy") or []
        if len(xyxy) >= 4:
            x1, y1, x2, y2 = [int(v) for v in xyxy]
            cv2.rectangle(out, (x1, y1), (x2, y2), (0, 220, 80), 2)
    gcx, gcy = det.get("grip_center_px") or det.get("bbox_center_px") or [0, 0]
    cv2.circle(out, (int(gcx), int(gcy)), 8, (255, 180, 0), -1)
    cv2.circle(out, (int(gcx), int(gcy)), 10, (255, 255, 255), 2)
    backend = det.get("backend") or "?"
    label = f"{det.get('label', 'obj')} {float(det.get('confidence', 0)):.2f} [{backend}]"
    cv2.putText(
        out,
        label,
        (12, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (0, 255, 120),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        out,
        f"centro presa {int(gcx)},{int(gcy)} px",
        (12, 52),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (200, 220, 255),
        1,
        cv2.LINE_AA,
    )
    orient = det.get("orientation_deg")
    short_deg = det.get("grip_align_deg")
    axis = det.get("orient_axis_px")
    if orient is not None:
        cv2.putText(
            out,
            f"lato lungo {float(orient):+.1f}°"
            + (f" · corto {float(short_deg):+.1f}°" if short_deg is not None else ""),
            (12, 76),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (255, 120, 220),
            1,
            cv2.LINE_AA,
        )
    if isinstance(axis, list) and len(axis) >= 2:
        p0 = axis[0]
        p1 = axis[1]
        if isinstance(p0, (list, tuple)) and isinstance(p1, (list, tuple)) and len(p0) >= 2 and len(p1) >= 2:
            cv2.arrowedLine(
                out,
                (int(p0[0]), int(p0[1])),
                (int(p1[0]), int(p1[1])),
                (255, 80, 255),
                3,
                tipLength=0.22,
                line_type=cv2.LINE_AA,
            )
            cv2.putText(
                out,
                "lungo",
                (int(p1[0]) + 6, int(p1[1]) - 4),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (255, 80, 255),
                1,
                cv2.LINE_AA,
            )
    grip_axis = det.get("grip_align_axis_px")
    if isinstance(grip_axis, list) and len(grip_axis) >= 2:
        g0, g1 = grip_axis[0], grip_axis[1]
        if isinstance(g0, (list, tuple)) and isinstance(g1, (list, tuple)) and len(g0) >= 2 and len(g1) >= 2:
            cv2.arrowedLine(
                out,
                (int(g0[0]), int(g0[1])),
                (int(g1[0]), int(g1[1])),
                (0, 220, 255),
                3,
                tipLength=0.22,
                line_type=cv2.LINE_AA,
            )
            cv2.putText(
                out,
                "corto · pinza",
                (int(g1[0]) + 6, int(g1[1]) - 4),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (0, 220, 255),
                1,
                cv2.LINE_AA,
            )
    return out


def _detect_on_frame(frame: Any, *, snapshot_name: str) -> dict[str, Any]:
    import sys

    scripts_dir = str(PROJECT_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from box_object_detector import detect_box_object, detector_status

    fh, fw = int(frame.shape[0]), int(frame.shape[1])
    crop, offset_xy, roi = pick_vision_crop.crop_frame_for_detection(frame)
    det = detect_box_object(crop)
    det = pick_vision_crop.map_detection_to_full_frame(
        det,
        offset_xy=offset_xy,
        crop_hw=(int(crop.shape[1]), int(crop.shape[0])),
        full_hw=(fw, fh),
    )
    det = pick_preset.stabilize_detection_orientation(det)
    det["detector_status"] = detector_status()
    base_overlay = pick_vision_crop.draw_crop_roi_outline(frame, roi)
    overlay = _draw_detection(base_overlay, det)
    try:
        import cv2
    except ImportError:
        return {"ok": False, "reason": "cv2_unavailable", "detection": det}

    quality = int(os.environ.get("D1_ORBBEC_JPEG_QUALITY", "88"))
    ok_enc, buf = cv2.imencode(".jpg", overlay, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok_enc or buf is None:
        return {"ok": False, "reason": "encode_failed", "detection": det}
    orbbec_capture._SNAP_DIR.mkdir(parents=True, exist_ok=True)
    _overlay_path().write_bytes(buf.tobytes())

    last_detection: dict[str, Any] = pick_preset.stabilize_detection_orientation(
        {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "snapshot": snapshot_name,
            "backend": det.get("backend"),
            "label": det.get("label"),
            "confidence": det.get("confidence"),
            "grip_center_px": det.get("grip_center_px"),
            "bbox_xyxy": det.get("bbox_xyxy"),
            "norm": det.get("norm"),
            "orientation_deg": det.get("orientation_deg"),
            "orient_axis_px": det.get("orient_axis_px"),
            "orient_box_px": det.get("orient_box_px"),
            "grip_align_deg": det.get("grip_align_deg"),
            "grip_align_axis_px": det.get("grip_align_axis_px"),
            "detect_method": det.get("detect_method"),
            "detected": bool(det.get("ok")),
        }
    )
    ts = int(time.time())
    return {
        "ok": True,
        "detection_ok": bool(det.get("ok")),
        "detection": det,
        "last_detection": last_detection,
        "preview_url": f"/api/pick/scene.jpg?t={ts}",
        "hint_it": (
            "Scatoletta blu in ROI — centro e rotazione da maschera colore; rifai foto se sposti il pezzo."
            if det.get("ok") and det.get("backend") == "color_blue_box"
            else (
                "Oggetto in ROI (senza pinza) — presa con offset calibrazione; se sposti il pezzo rifai foto."
                if det.get("ok")
                else "Nessun blu in ROI — regola crop, luce, o D1_COLOR_BOX_H_MIN/MAX in env."
            )
        ),
    }


def capture_and_detect() -> dict[str, Any]:
    """Nuova foto RGB + rilevamento sulla stessa immagine (una sola operazione)."""
    cap = _capture_wrist_rgb_jpeg()
    if not cap.get("ok"):
        return {
            "ok": False,
            "reason": "capture_failed",
            "capture": cap,
            "hint": cap.get("hint") or cap.get("reason"),
        }
    snap = orbbec_capture.latest_snapshot_path()
    if snap is None or not snap.is_file():
        return {"ok": False, "reason": "no_snapshot", "capture": cap}
    frame = _read_bgr_from_jpeg(snap)
    if frame is None:
        return {"ok": False, "reason": "cv2_unavailable_or_decode_failed", "capture": cap}
    out = _detect_on_frame(frame, snapshot_name=snap.name)
    if not out.get("ok"):
        out["capture"] = cap
        return out
    out["capture"] = cap
    out["image_url"] = cap.get("image_url")
    return out


def _capture_wrist_rgb_jpeg() -> dict[str, Any]:
    try:
        from go2_dashboard.cameras import CAMERA_CACHE, wrist_depth_backend
    except Exception:
        return orbbec_capture.capture_orbbec_jpeg()

    if wrist_depth_backend() != "realsense":
        return orbbec_capture.capture_orbbec_jpeg()

    CAMERA_CACHE.start(0)
    jpg = CAMERA_CACHE.get_jpeg(0, wait_s=float(os.environ.get("D1_PICK_REALSENSE_WAIT_S", "3.0")))
    if not jpg:
        return {
            "ok": False,
            "reason": "realsense_wrist_capture_failed",
            "hint": "Nessun frame RGB dalla RealSense D456 polso (logical 0). Controlla /api/cameras/status.",
            "stream_kind": "rgb",
            "via": "camera_cache_logical_0",
        }
    return orbbec_capture._save_jpeg(
        jpg,
        source="camera_cache:logical0:wrist_realsense_d456",
        extra={
            "via": "camera_cache_logical_0",
            "camera_backend": "realsense",
            "stream_kind": "rgb_cache",
        },
    )


def detect_on_latest_snapshot(*, capture_if_missing: bool = True) -> dict[str, Any]:
    """Compat: se manca foto fa capture_and_detect, altrimenti detect su latest."""
    snap = orbbec_capture.latest_snapshot_path()
    if snap is None and capture_if_missing:
        return capture_and_detect()
    if snap is None or not snap.is_file():
        return {"ok": False, "reason": "no_snapshot", "hint": "Premi «Foto e cerca oggetto»"}

    frame = _read_bgr_from_jpeg(snap)
    if frame is None:
        return {"ok": False, "reason": "cv2_unavailable_or_decode_failed"}
    return _detect_on_frame(frame, snapshot_name=snap.name)

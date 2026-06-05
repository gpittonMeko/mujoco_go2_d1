"""Point cloud da RGB + depth JPEG (UVC) per GraspGen ZMQ."""

from __future__ import annotations

from typing import Any

import numpy as np


def decode_depth_bgr(jpeg_bytes: bytes) -> np.ndarray | None:
    import cv2

    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    if frame is None:
        return None
    if frame.ndim == 3:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return frame


def bbox_point_cloud(
    depth_gray: np.ndarray,
    bbox_xyxy: list[float],
    *,
    fx: float,
    fy: float,
    cx: float,
    cy: float,
    depth_scale_m_per_unit: float = 0.001,
    max_points: int = 8192,
) -> np.ndarray:
    """Campiona punti 3D camera frame nella ROI bbox (per GraspGen)."""
    h, w = depth_gray.shape[:2]
    x1, y1, x2, y2 = [int(round(float(v))) for v in bbox_xyxy[:4]]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w - 1, x2), min(h - 1, y2)
    if x2 <= x1 or y2 <= y1:
        return np.zeros((0, 3), dtype=np.float32)
    roi = depth_gray[y1:y2, x1:x2]
    if roi.size == 0:
        return np.zeros((0, 3), dtype=np.float32)
    if np.issubdtype(roi.dtype, np.floating):
        zm = roi.astype(np.float32) * float(depth_scale_m_per_unit)
    else:
        zm = roi.astype(np.float32) * float(depth_scale_m_per_unit)
    valid = zm > 0.05
    if not np.any(valid):
        return np.zeros((0, 3), dtype=np.float32)
    ys, xs = np.where(valid)
    zs = zm[valid]
    u = (xs + x1).astype(np.float32)
    v = (ys + y1).astype(np.float32)
    xs3 = ((u - cx) / max(fx, 1.0)) * zs
    ys3 = ((v - cy) / max(fy, 1.0)) * zs
    pts = np.stack([xs3, ys3, zs], axis=1)
    if len(pts) > max_points:
        idx = np.linspace(0, len(pts) - 1, max_points, dtype=int)
        pts = pts[idx]
    return pts.astype(np.float32)


def point_cloud_from_rgbd(
    rgb_bgr: np.ndarray,
    depth_jpeg: bytes | None,
    detection: dict[str, Any],
    *,
    depth_scale_m_per_unit: float | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    meta: dict[str, Any] = {"ok": False, "num_points": 0}
    if not detection.get("ok"):
        meta["reason"] = "no_detection"
        return np.zeros((0, 3), dtype=np.float32), meta
    bbox = detection.get("bbox_xyxy") or []
    if len(bbox) < 4:
        meta["reason"] = "no_bbox"
        return np.zeros((0, 3), dtype=np.float32), meta
    if not depth_jpeg:
        meta["reason"] = "no_depth"
        return np.zeros((0, 3), dtype=np.float32), meta
    depth = decode_depth_bgr(depth_jpeg)
    if depth is None:
        meta["reason"] = "depth_decode_failed"
        return np.zeros((0, 3), dtype=np.float32), meta
    h, w = rgb_bgr.shape[:2]
    if depth.shape[:2] != (h, w):
        import cv2

        depth = cv2.resize(depth, (w, h), interpolation=cv2.INTER_NEAREST)
    fx = fy = max(w, h) * 0.9
    cx, cy = w / 2.0, h / 2.0
    scale = 0.001 if depth_scale_m_per_unit is None else float(depth_scale_m_per_unit)
    pts = bbox_point_cloud(depth, bbox, fx=fx, fy=fy, cx=cx, cy=cy, depth_scale_m_per_unit=scale)
    meta["ok"] = len(pts) >= 32
    meta["num_points"] = int(len(pts))
    return pts, meta

"""RGB + depth V4L per payload worker cloud (JPEG base64)."""

from __future__ import annotations

import base64
import os
from typing import Any


def capture_rgbd_b64(logical: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "logical_camera_device": int(logical),
        "rgb_ok": False,
        "depth_ok": False,
    }
    try:
        from go2_dashboard.cameras import CAMERA_CACHE, debug_v4l_snapshot_jpeg
        from go2_dashboard.blueprints.operator_api.helpers_camera import _depth_v4l_index_for_logical_camera
        from go2_dashboard.operator_stack import go2_local

        if go2_local():
            rgb = CAMERA_CACHE.get_jpeg(int(logical))
            if rgb:
                out["jpeg_base64"] = base64.standard_b64encode(rgb).decode("ascii")
                out["rgb_ok"] = True
        didx = _depth_v4l_index_for_logical_camera(int(logical))
        if didx is not None:
            depth_raw = debug_v4l_snapshot_jpeg(didx, jpeg_quality=52)
            if depth_raw:
                out["depth_jpeg_b64"] = base64.standard_b64encode(depth_raw).decode("ascii")
                out["depth_v4l_index"] = didx
                out["depth_ok"] = True
        else:
            out["depth_skip_reason"] = "no_GO2_DEPTH_VIDEO_INDEX"
    except Exception as exc:
        out["capture_error"] = repr(exc)
    scale = (os.environ.get("GO2_DEPTH_SCALE_M_PER_UNIT") or "").strip()
    if scale:
        try:
            out["depth_scale_m_per_unit"] = float(scale)
        except ValueError:
            pass
    return out


def embed_rgbd_into_plan_body(body: dict[str, Any]) -> dict[str, Any]:
    """Aggiunge jpeg/depth inline se mancanti (cloud o richiesta esplicita)."""
    out = dict(body)
    if out.get("jpeg_base64") and out.get("depth_jpeg_b64"):
        return out
    logical = int(out.get("logical_camera_device") or 0)
    snap = capture_rgbd_b64(logical)
    if snap.get("jpeg_base64") and not out.get("jpeg_base64"):
        out["jpeg_base64"] = snap["jpeg_base64"]
    if snap.get("depth_jpeg_b64") and not out.get("depth_jpeg_b64"):
        out["depth_jpeg_b64"] = snap["depth_jpeg_b64"]
    if snap.get("depth_scale_m_per_unit") is not None:
        out.setdefault("depth_scale_m_per_unit", snap["depth_scale_m_per_unit"])
    if snap.get("depth_v4l_index") is not None:
        out["depth_v4l_index"] = snap["depth_v4l_index"]
    out["rgbd_embedded"] = bool(snap.get("rgb_ok"))
    out["depth_embedded"] = bool(snap.get("depth_ok"))
    return out

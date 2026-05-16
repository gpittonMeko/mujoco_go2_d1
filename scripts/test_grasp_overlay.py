#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import numpy as np

from box_grasp_planner import draw_grasp_overlay, plan_from_frame


def main() -> int:
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    fake_det = {
        "ok": True,
        "backend": "test",
        "confidence": 0.91,
        "bbox_xyxy": [72.0, 56.0, 228.0, 188.0],
        "bbox_center_px": [150.0, 120.0],
        "grip_center_px": [150.0, 120.0],
        "grip_axis_px": [[72.0, 120.0], [228.0, 120.0]],
    }
    plan = plan_from_frame(frame, object_detection=fake_det, logical_camera_device=0)
    assert plan.get("logical_camera_device") == 0
    base_xyz = (plan.get("target") or {}).get("base_xyz_m") or []
    assert len(base_xyz) >= 3, base_xyz
    overlay = draw_grasp_overlay(frame, plan)
    assert overlay.shape == frame.shape, (overlay.shape, frame.shape)
    assert overlay.dtype == frame.dtype, (overlay.dtype, frame.dtype)
    assert int(np.abs(overlay.astype(np.int16) - frame.astype(np.int16)).sum()) > 0, "overlay should draw visible annotations"

    os.environ["GO2_BOX_TARGET_OFFSET_LOGICAL_0_M"] = "0.01,0.02,0.03"
    try:
        shifted = plan_from_frame(frame, object_detection=fake_det, logical_camera_device=0)
        xyz = (shifted.get("target") or {}).get("base_xyz_m") or []
        assert len(xyz) >= 3, xyz
        assert abs((float(xyz[0]) - float(base_xyz[0])) - 0.01) < 1e-3, (base_xyz, xyz)
        assert abs((float(xyz[1]) - float(base_xyz[1])) - 0.02) < 1e-3, (base_xyz, xyz)
        assert abs((float(xyz[2]) - float(base_xyz[2])) - 0.03) < 1e-3, (base_xyz, xyz)
    finally:
        os.environ.pop("GO2_BOX_TARGET_OFFSET_LOGICAL_0_M", None)
    print("GRASP_OVERLAY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

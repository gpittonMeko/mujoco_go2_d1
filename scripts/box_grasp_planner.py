#!/usr/bin/env python3
"""
AprilTag-assisted dry-run planner for a floor box grasp with the D1 arm.

This module intentionally does not publish robot commands. It detects tag25h9
tags 0..3, estimates a conservative target in the D1 workspace, runs the
existing D1 IK, and returns a preview trajectory for dashboard validation.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from arm_kinematics_d1_template import ARM_FOLD_POSE, fk_tool_tip, ik_reach, smooth


TAG_IDS = {0, 1, 2, 3}
DEFAULT_TAG_SIZE_M = float(os.environ.get("BOX_TAG_SIZE_M", "0.06"))
DEFAULT_CAMERA_HEIGHT_M = float(os.environ.get("BOX_CAMERA_HEIGHT_M", "0.34"))


@dataclass
class CameraModel:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float

    @classmethod
    def from_frame(cls, frame: np.ndarray) -> "CameraModel":
        height, width = frame.shape[:2]
        fx = float(os.environ.get("REALSENSE_FX", width * 0.96))
        fy = float(os.environ.get("REALSENSE_FY", width * 0.96))
        cx = float(os.environ.get("REALSENSE_CX", width / 2.0))
        cy = float(os.environ.get("REALSENSE_CY", height / 2.0))
        return cls(width=width, height=height, fx=fx, fy=fy, cx=cx, cy=cy)

    @property
    def matrix(self) -> np.ndarray:
        return np.array(
            [[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]],
            dtype=np.float32,
        )


def _aruco_detector() -> tuple[Any, Any] | tuple[None, None]:
    if not hasattr(cv2, "aruco"):
        return None, None
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_25H9)
    params = cv2.aruco.DetectorParameters()
    if hasattr(cv2.aruco, "CORNER_REFINE_APRILTAG"):
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_APRILTAG
    if hasattr(cv2.aruco, "ArucoDetector"):
        return cv2.aruco.ArucoDetector(dictionary, params), None
    return dictionary, params


def detect_box_tags(frame: np.ndarray) -> dict[str, Any]:
    detector, params = _aruco_detector()
    if detector is None:
        return {"ok": False, "error": "OpenCV aruco AprilTag support unavailable", "tags": []}

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if params is None:
        corners, ids, _ = detector.detectMarkers(gray)
    else:
        corners, ids, _ = cv2.aruco.detectMarkers(gray, detector, parameters=params)

    tags = []
    if ids is not None:
        for idx, tag_id_arr in enumerate(ids):
            tag_id = int(tag_id_arr[0])
            if tag_id not in TAG_IDS:
                continue
            pts = corners[idx].reshape(4, 2).astype(float)
            center = pts.mean(axis=0)
            tags.append(
                {
                    "id": tag_id,
                    "center_px": [round(float(center[0]), 1), round(float(center[1]), 1)],
                    "corners_px": [[round(float(x), 1), round(float(y), 1)] for x, y in pts],
                }
            )
    return {"ok": bool(tags), "tags": tags, "tag_family": "tag25h9", "expected_ids": sorted(TAG_IDS)}


def estimate_tag_poses(frame: np.ndarray, tags_result: dict[str, Any]) -> dict[str, Any]:
    if not tags_result.get("ok"):
        return {"ok": False, "error": tags_result.get("error", "no tags"), "poses": []}

    cam = CameraModel.from_frame(frame)
    dist = np.zeros((5, 1), dtype=np.float32)
    half = DEFAULT_TAG_SIZE_M / 2.0
    object_points = np.array(
        [
            [-half, half, 0.0],
            [half, half, 0.0],
            [half, -half, 0.0],
            [-half, -half, 0.0],
        ],
        dtype=np.float32,
    )
    poses = []
    for tag in tags_result["tags"]:
        corners = np.array(tag["corners_px"], dtype=np.float32).reshape(1, 4, 2)
        try:
            if hasattr(cv2.aruco, "estimatePoseSingleMarkers"):
                _rvecs, tvecs, _ = cv2.aruco.estimatePoseSingleMarkers(
                    corners, DEFAULT_TAG_SIZE_M, cam.matrix, dist
                )
                t = tvecs[0][0].astype(float)
            else:
                ok, _rvec, tvec = cv2.solvePnP(
                    object_points,
                    corners.reshape(4, 2),
                    cam.matrix,
                    dist,
                    flags=cv2.SOLVEPNP_IPPE_SQUARE if hasattr(cv2, "SOLVEPNP_IPPE_SQUARE") else cv2.SOLVEPNP_ITERATIVE,
                )
                if not ok:
                    return {"ok": False, "error": "pose estimate failed: solvePnP returned false", "poses": []}
                t = tvec.reshape(3).astype(float)
        except Exception as exc:
            return {"ok": False, "error": f"pose estimate failed: {exc!r}", "poses": []}
        poses.append(
            {
                "id": tag["id"],
                "camera_xyz_m": [round(float(t[0]), 4), round(float(t[1]), 4), round(float(t[2]), 4)],
                "range_m": round(float(np.linalg.norm(t)), 4),
            }
        )
    return {"ok": bool(poses), "camera_model": cam.__dict__, "tag_size_m": DEFAULT_TAG_SIZE_M, "poses": poses}


def estimate_box_target_base(poses_result: dict[str, Any]) -> dict[str, Any]:
    """
    Conservative camera-to-base approximation for preview.

    Real calibration should replace this with a measured transform. For now:
    camera x = right, y = down, z = forward
    base x = forward, y = left, z = height above ground/arm base heuristic
    """
    poses = poses_result.get("poses") or []
    if not poses:
        return {"ok": False, "error": "no pose estimates"}

    xyz = np.array([p["camera_xyz_m"] for p in poses], dtype=float)
    mean = xyz.mean(axis=0)
    cam_x, cam_y, cam_z = mean
    target_x = float(np.clip(cam_z, 0.18, 0.72))
    target_y = float(np.clip(-cam_x, -0.35, 0.35))
    # Aim a little above floor, then approach down. The tag center y helps infer if
    # the box is lower in the image, but we keep z conservative until calibrated.
    target_z = float(np.clip(DEFAULT_CAMERA_HEIGHT_M - cam_y - 0.10, 0.04, 0.22))
    return {
        "ok": True,
        "base_xyz_m": [round(target_x, 4), round(target_y, 4), round(target_z, 4)],
        "calibration": "approximate camera-to-base transform; dry-run only",
        "source_tag_count": len(poses),
    }


def build_grasp_preview(target_base: dict[str, Any]) -> dict[str, Any]:
    if not target_base.get("ok"):
        return {"ok": False, "error": target_base.get("error", "no target")}

    x, y, z = target_base["base_xyz_m"]
    stages = [
        ("pre_grasp", x - 0.06, y, z + 0.12),
        ("approach", x - 0.015, y, z + 0.045),
        ("grasp", x, y, z + 0.02),
        ("lift", x - 0.04, y, z + 0.18),
    ]
    plan = []
    previous = list(ARM_FOLD_POSE)
    for name, sx, sy, sz in stages:
        q = ik_reach(sx, sy, sz)
        if q is None:
            return {"ok": False, "failed_stage": name, "target_xyz_m": [sx, sy, sz]}
        tip = fk_tool_tip(q)
        plan.append(
            {
                "stage": name,
                "target_xyz_m": [round(float(sx), 4), round(float(sy), 4), round(float(sz), 4)],
                "joints_rad": [round(float(v), 4) for v in q],
                "fk_tip_xyz_m": [round(float(v), 4) for v in tip],
            }
        )
        previous = smooth(previous, q, 0.5)

    gripper = [
        {"stage": "pre_grasp", "gripper": "open"},
        {"stage": "grasp", "gripper": "close", "hold_s": 0.6},
        {"stage": "lift", "gripper": "hold_closed"},
    ]
    return {"ok": True, "mode": "dry-run", "plan": plan, "gripper": gripper}


def plan_from_frame(frame: np.ndarray) -> dict[str, Any]:
    tags = detect_box_tags(frame)
    poses = estimate_tag_poses(frame, tags)
    target = estimate_box_target_base(poses)
    preview = build_grasp_preview(target)
    return {"ok": bool(preview.get("ok")), "tags": tags, "poses": poses, "target": target, "preview": preview}


def draw_tags(frame: np.ndarray, tags_result: dict[str, Any]) -> np.ndarray:
    out = frame.copy()
    for tag in tags_result.get("tags", []):
        pts = np.array(tag["corners_px"], dtype=np.int32)
        cv2.polylines(out, [pts], True, (0, 255, 0), 2)
        cx, cy = map(int, tag["center_px"])
        cv2.putText(out, f"id {tag['id']}", (cx + 6, cy - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
    return out


if __name__ == "__main__":
    image_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not image_path:
        raise SystemExit("usage: box_grasp_planner.py image.jpg")
    frame = cv2.imread(image_path)
    if frame is None:
        raise SystemExit(f"cannot read image: {image_path}")
    import json

    print(json.dumps(plan_from_frame(frame), indent=2))

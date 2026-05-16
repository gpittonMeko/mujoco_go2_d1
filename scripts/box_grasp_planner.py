#!/usr/bin/env python3
"""Planner AprilTag + IK presa (no comandi robot). Tag 0–3 scatola, tag 5 landmark; edge m per ID da env."""


from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import cv2
import numpy as np

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from arm_kinematics_d1_template import fk_tool_tip, ik_reach


# Tags glued on / near the grasp object (tag25h9 family).
BOX_TAG_IDS = frozenset({0, 1, 2, 3})

# Fixed landmark on the robot stack (tag25h9 ID 5 — **visible mainly / only from wrist cam**
# ``/dev/video0``): mounted above / near the XT-16 LiDAR plane so cameras (ahead) and scene
# objects (further ahead) are interpretable relative to base + arm.
REFERENCE_TAG_ID_LIDAR_FRAME = 5

TRACKED_TAG_IDS = BOX_TAG_IDS | {REFERENCE_TAG_ID_LIDAR_FRAME}

# Back-compat name used by JSON consumers ("which IDs we look for in frame").
TAG_IDS = TRACKED_TAG_IDS
# Physical square **edge** lengths (meters) for pose estimation — tags differ by placement.
BOX_TAG_SIZE_M = float(os.environ.get("BOX_TAG_SIZE_M", "0.019"))
REFERENCE_TAG_SIZE_M = float(
    os.environ.get("REFERENCE_TAG_SIZE_M", os.environ.get("LIDAR_LANDMARK_TAG_SIZE_M", "0.060"))
)

# Deprecated alias (single-size legacy); prefer BOX_TAG_SIZE_M / REFERENCE_TAG_SIZE_M.
DEFAULT_TAG_SIZE_M = BOX_TAG_SIZE_M
DEFAULT_CAMERA_HEIGHT_M = float(os.environ.get("BOX_CAMERA_HEIGHT_M", "0.34"))


def tag_edge_length_m(tag_id: int) -> float:
    """Outer edge length in meters for solvePnP / estimatePoseSingleMarkers."""
    tid = int(tag_id)
    if tid == REFERENCE_TAG_ID_LIDAR_FRAME:
        return REFERENCE_TAG_SIZE_M
    if tid in BOX_TAG_IDS:
        return BOX_TAG_SIZE_M
    return BOX_TAG_SIZE_M


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


def detect_box_tags(
    frame: np.ndarray, *, include_tag_ids: frozenset[int] | None = None
) -> dict[str, Any]:
    detector, params = _aruco_detector()
    if detector is None:
        return {"ok": False, "error": "OpenCV aruco AprilTag support unavailable", "tags": []}

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if params is None:
        corners, ids, _ = detector.detectMarkers(gray)
    else:
        corners, ids, _ = cv2.aruco.detectMarkers(gray, detector, parameters=params)

    tags = []
    allowed = set(TRACKED_TAG_IDS)
    if include_tag_ids:
        allowed |= set(include_tag_ids)
    if ids is not None:
        for idx, tag_id_arr in enumerate(ids):
            tag_id = int(tag_id_arr[0])
            if tag_id not in allowed:
                continue
            pts = corners[idx].reshape(4, 2).astype(float)
            center = pts.mean(axis=0)
            d01 = float(np.linalg.norm(pts[0] - pts[2]))
            d12 = float(np.linalg.norm(pts[1] - pts[3]))
            diagonal_px = round((d01 + d12) / 2.0, 2)
            edges = [float(np.linalg.norm(pts[i] - pts[(i + 1) % 4])) for i in range(4)]
            mean_edge_px = round(float(np.mean(edges)), 2)
            tags.append(
                {
                    "id": tag_id,
                    "center_px": [round(float(center[0]), 1), round(float(center[1]), 1)],
                    "corners_px": [[round(float(x), 1), round(float(y), 1)] for x, y in pts],
                    "diagonal_px": diagonal_px,
                    "mean_edge_px": mean_edge_px,
                }
            )
    return {"ok": bool(tags), "tags": tags, "tag_family": "tag25h9", "expected_ids": sorted(allowed)}


def estimate_tag_poses(
    frame: np.ndarray,
    tags_result: dict[str, Any],
    *,
    tag_edge_length_overrides: dict[int, float] | None = None,
) -> dict[str, Any]:
    if not tags_result.get("ok"):
        return {"ok": False, "error": tags_result.get("error", "no tags"), "poses": []}

    cam = CameraModel.from_frame(frame)
    dist = np.zeros((5, 1), dtype=np.float32)
    poses = []
    for tag in tags_result["tags"]:
        tid = int(tag["id"])
        if tag_edge_length_overrides and tid in tag_edge_length_overrides:
            edge_m = float(tag_edge_length_overrides[tid])
        else:
            edge_m = tag_edge_length_m(tid)
        half = edge_m / 2.0
        object_points = np.array(
            [
                [-half, half, 0.0],
                [half, half, 0.0],
                [half, -half, 0.0],
                [-half, -half, 0.0],
            ],
            dtype=np.float32,
        )
        corners = np.array(tag["corners_px"], dtype=np.float32).reshape(1, 4, 2)
        try:
            if hasattr(cv2.aruco, "estimatePoseSingleMarkers"):
                _rvecs, tvecs, _ = cv2.aruco.estimatePoseSingleMarkers(
                    corners, edge_m, cam.matrix, dist
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
                "tag_edge_length_m": round(edge_m, 6),
                "camera_xyz_m": [round(float(t[0]), 4), round(float(t[1]), 4), round(float(t[2]), 4)],
                "range_m": round(float(np.linalg.norm(t)), 4),
            }
        )
    return {
        "ok": bool(poses),
        "camera_model": cam.__dict__,
        "tag_sizes_m": {
            "box_tags": BOX_TAG_SIZE_M,
            "reference_tag_id": REFERENCE_TAG_ID_LIDAR_FRAME,
            "reference_tag": REFERENCE_TAG_SIZE_M,
        },
        "poses": poses,
    }


def _default_tag5_calibration_path() -> Path:
    return SCRIPTS_DIR.parent / "data" / "tag5_calibration_arm_base.json"


def tag5_calibration_offset_arm_base_m(logical_camera_device: int | None = None) -> list[float] | None:
    """
    Offset additivo (m) in frame base braccio, da file scritto da calibrazione.

    Se il JSON contiene ``offset_by_logical_camera_device_m`` con la chiave del device
    (stringa ``"0"`` o ``"6"``), usa quell'offset per quella telecamera; altrimenti usa
    ``offset_arm_base_m`` (calibrazione landmark tag 5 classica, stesso offset per tutte).
    Disattivabile con ``GO2_TAG5_CALIBRATION_ENABLE=0``.
    """
    if os.environ.get("GO2_TAG5_CALIBRATION_ENABLE", "1").lower() in {"0", "false", "no"}:
        return None
    path = os.environ.get("GO2_TAG5_CALIBRATION_JSON", "").strip()
    p = Path(path).expanduser() if path else _default_tag5_calibration_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if logical_camera_device is not None:
            by_dev = data.get("offset_by_logical_camera_device_m")
            if isinstance(by_dev, dict):
                key = str(int(logical_camera_device))
                off_d = by_dev.get(key)
                if isinstance(off_d, list) and len(off_d) >= 3:
                    return [float(off_d[0]), float(off_d[1]), float(off_d[2])]
        off = data.get("offset_arm_base_m")
        if isinstance(off, list) and len(off) >= 3:
            return [float(off[0]), float(off[1]), float(off[2])]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


def _camera_tvec_to_base_heuristic_xyz(camera_xyz_m: Sequence[float]) -> list[float]:
    """
    Euristica tvec OpenCV → base braccio **senza** offset calibrazione tag 5.
    Usata per calcolare l'offset: nominal - heuristic(tag5).
    """
    if len(camera_xyz_m) < 3:
        return [0.18, 0.0, 0.12]
    sx = float(os.environ.get("GO2_BOX_TVEC_SIGN_X", "1"))
    sy = float(os.environ.get("GO2_BOX_TVEC_SIGN_Y", "1"))
    sz = float(os.environ.get("GO2_BOX_TVEC_SIGN_Z", "1"))
    cam_x = float(camera_xyz_m[0]) * sx
    cam_y = float(camera_xyz_m[1]) * sy
    cam_z = float(camera_xyz_m[2]) * sz
    fwd = cam_z
    if os.environ.get("GO2_BOX_TARGET_NEGATE_FORWARD", "0").lower() in {"1", "true", "yes"}:
        fwd = -fwd
    lat = -cam_x
    if os.environ.get("GO2_BOX_TARGET_NEGATE_LATERAL", "0").lower() in {"1", "true", "yes"}:
        lat = -lat
    target_x = float(np.clip(fwd, 0.18, 0.72))
    target_y = float(np.clip(lat, -0.35, 0.35))
    height_off = float(os.environ.get("GO2_BOX_TARGET_HEIGHT_OFFSET_M", "0.10"))
    ht = DEFAULT_CAMERA_HEIGHT_M - cam_y - height_off
    if os.environ.get("GO2_BOX_TARGET_NEGATE_HEIGHT_TERM", "0").lower() in {"1", "true", "yes"}:
        ht = DEFAULT_CAMERA_HEIGHT_M + cam_y - height_off
    target_z = float(np.clip(ht, 0.04, 0.22))
    return [target_x, target_y, target_z]


def camera_device_preview_offset_m(logical_camera_device: int | None) -> list[float]:
    """Offset additivo per preview target/grasp per camera, configurabile da env senza toccare il landmark tag5."""
    if logical_camera_device is None:
        return [0.0, 0.0, 0.0]
    raw = os.environ.get(f"GO2_BOX_TARGET_OFFSET_LOGICAL_{int(logical_camera_device)}_M", "").strip()
    if not raw:
        return [0.0, 0.0, 0.0]
    try:
        vals = [float(x.strip()) for x in raw.split(",")]
        if len(vals) >= 3:
            return [vals[0], vals[1], vals[2]]
    except ValueError:
        pass
    return [0.0, 0.0, 0.0]


def camera_tvec_to_base_xyz(
    camera_xyz_m: Sequence[float],
    *,
    logical_camera_device: int | None = None,
    apply_tag5_calibration: bool = True,
) -> list[float]:
    """
    tvec OpenCV (centro tag) → stima nel frame **base braccio** (arm_link00 / FK).
    Se ``apply_tag5_calibration`` è True, applica l'offset da file calibrazione (solo per il
    landmark tag 5: è ``nominal - heuristic(tag5)`` e **non** va sommato ai tag scatola 0–3).
    Per gli altri target può applicare un offset preview specifico per camera
    (``GO2_BOX_TARGET_OFFSET_LOGICAL_<dev>_M=x,y,z``).
    """
    h = _camera_tvec_to_base_heuristic_xyz(camera_xyz_m)
    if apply_tag5_calibration:
        off = tag5_calibration_offset_arm_base_m(logical_camera_device)
        if off is not None:
            return [h[0] + off[0], h[1] + off[1], h[2] + off[2]]
    cam_off = camera_device_preview_offset_m(logical_camera_device)
    return [h[0] + cam_off[0], h[1] + cam_off[1], h[2] + cam_off[2]]


def make_tag5_calibration_record(
    nominal_tag5_arm_base_m: Sequence[float],
    tag5_camera_xyz_m: Sequence[float],
    *,
    logical_camera_device: int,
) -> dict[str, Any]:
    """
    Crea il record da salvare in ``data/tag5_calibration_arm_base.json``:
    salva sia il **nominale fisico** del tag 5 sia la **correzione interna**
    ``offset = nominal - heuristic(tag5)`` nello stesso frame (base braccio).
    """
    h = _camera_tvec_to_base_heuristic_xyz(
        [float(tag5_camera_xyz_m[0]), float(tag5_camera_xyz_m[1]), float(tag5_camera_xyz_m[2])]
    )
    off = [float(nominal_tag5_arm_base_m[i]) - h[i] for i in range(3)]
    return {
        "offset_arm_base_m": [round(off[0], 6), round(off[1], 6), round(off[2], 6)],
        "nominal_tag5_arm_base_m": [round(float(nominal_tag5_arm_base_m[i]), 6) for i in range(3)],
        "tag5_camera_xyz_m_used": [round(float(tag5_camera_xyz_m[i]), 6) for i in range(3)],
        "heuristic_base_before_m": [round(h[i], 6) for i in range(3)],
        "logical_camera_device": int(logical_camera_device),
        "reference_tag_id": REFERENCE_TAG_ID_LIDAR_FRAME,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Offset ``offset_arm_base_m`` da landmark tag 5 (tipicamente polso). "
            "Se presente ``offset_by_logical_camera_device_m``, quello ha priorità per device 0/6."
        ),
    }


def tvec_camera_m_for_tag_id(
    frame: np.ndarray,
    tag_id: int,
    *,
    tag_edge_length_overrides: dict[int, float] | None = None,
) -> list[float] | None:
    """
    tvec OpenCV (centro tag) nel frame camera per ``tag_id`` se rilevato nel frame.
    ``tag_edge_length_overrides`` imposta il lato (m) per ID non standard (0–3/5 usano le costanti).
    """
    tid = int(tag_id)
    tags = detect_box_tags(frame, include_tag_ids=frozenset({tid}))
    poses = estimate_tag_poses(frame, tags, tag_edge_length_overrides=tag_edge_length_overrides)
    for p in poses.get("poses") or []:
        if int(p.get("id", -1)) != tid:
            continue
        c = p.get("camera_xyz_m")
        if isinstance(c, list) and len(c) >= 3:
            return [float(c[0]), float(c[1]), float(c[2])]
    return None


def apriltag_tag_estimates_base_m(poses_result: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Per ogni tag con pose solvePnP, stima la posizione del centro tag nel frame base braccio
    (vis 3D). Include anche il landmark ID 5 se presente.
    """
    if not poses_result.get("ok"):
        return []
    out: list[dict[str, Any]] = []
    for p in poses_result.get("poses") or []:
        cam = p.get("camera_xyz_m")
        if not isinstance(cam, list) or len(cam) < 3:
            continue
        raw = [float(cam[0]), float(cam[1]), float(cam[2])]
        dev = None
        if isinstance(p.get("logical_camera_device"), (int, float)):
            try:
                dev = int(p["logical_camera_device"])
            except (TypeError, ValueError):
                dev = None
        tid = int(p.get("id", -1))
        base = camera_tvec_to_base_xyz(
            raw,
            logical_camera_device=dev,
            apply_tag5_calibration=(tid == REFERENCE_TAG_ID_LIDAR_FRAME),
        )
        out.append(
            {
                "id": int(p.get("id", -1)),
                "base_xyz_m": [round(base[i], 5) for i in range(3)],
                "range_m": p.get("range_m"),
                "camera_xyz_m": [round(raw[i], 5) for i in range(3)],
                "logical_camera_device": dev,
            }
        )
    return out


def estimate_box_target_base(poses_result: dict[str, Any]) -> dict[str, Any]:
    """
    Conservative camera-to-base approximation for preview.

    Uses **only** poses whose tag id is in ``BOX_TAG_IDS``. Landmark tag
    ``REFERENCE_TAG_ID_LIDAR_FRAME`` is detected for overlays but excluded here.

    OpenCV ``tvec`` is the tag center position in the **camera** frame (typical: x
    right, y down, z along optical axis). The mapping below is a heuristic; mounting
    differs per robot/camera. Tune without code edits:

    - ``GO2_BOX_TVEC_SIGN_X``, ``GO2_BOX_TVEC_SIGN_Y``, ``GO2_BOX_TVEC_SIGN_Z`` (default 1)
    - ``GO2_BOX_TARGET_NEGATE_FORWARD``, ``GO2_BOX_TARGET_NEGATE_LATERAL`` (0/1) — flips
      after sign multipliers; use if the arm moves **away** from the tag instead of toward it.
    """
    poses = poses_result.get("poses") or []
    poses_grasp = [p for p in poses if int(p.get("id", -1)) in BOX_TAG_IDS]
    if not poses_grasp:
        return {"ok": False, "error": "no pose estimates for box tags (ids in BOX_TAG_IDS)"}

    xyz = np.array([p["camera_xyz_m"] for p in poses_grasp], dtype=float)
    mean = xyz.mean(axis=0)
    sx = float(os.environ.get("GO2_BOX_TVEC_SIGN_X", "1"))
    sy = float(os.environ.get("GO2_BOX_TVEC_SIGN_Y", "1"))
    sz = float(os.environ.get("GO2_BOX_TVEC_SIGN_Z", "1"))
    cam_x = float(mean[0]) * sx
    cam_y = float(mean[1]) * sy
    cam_z = float(mean[2]) * sz

    dev: int | None = None
    for p in poses_grasp:
        if isinstance(p.get("logical_camera_device"), (int, float)):
            try:
                dev = int(p["logical_camera_device"])
                break
            except (TypeError, ValueError):
                pass

    tb = camera_tvec_to_base_xyz(
        [float(mean[0]), float(mean[1]), float(mean[2])],
        logical_camera_device=dev,
        apply_tag5_calibration=False,
    )
    target_x, target_y, target_z = tb[0], tb[1], tb[2]
    return {
        "ok": True,
        "base_xyz_m": [round(target_x, 4), round(target_y, 4), round(target_z, 4)],
        "calibration": (
            "heuristic tvec→base; GO2_BOX_TVEC_SIGN_* / GO2_BOX_TARGET_NEGATE_* env if arm diverges from tag"
        ),
        "source_tag_count": len(poses_grasp),
        "camera_xyz_mean_signed": [round(cam_x, 5), round(cam_y, 5), round(cam_z, 5)],
    }


def grip_point_from_tags(tags_result: dict[str, Any], frame_shape: tuple[int, int] | None = None) -> dict[str, Any]:
    tags = tags_result.get("tags") or []
    box_tags = [t for t in tags if int(t.get("id", -1)) in BOX_TAG_IDS]
    if not box_tags:
        return {"ok": False, "source": "apriltag", "reason": "no_box_tags"}
    centers = [t.get("center_px") for t in box_tags if t.get("center_px")]
    if not centers:
        return {"ok": False, "source": "apriltag", "reason": "no_centers"}
    xs = [float(c[0]) for c in centers]
    ys = [float(c[1]) for c in centers]
    all_pts: list[list[float]] = []
    for t in box_tags:
        all_pts.extend(t.get("corners_px") or [])
    if all_pts:
        px = [float(p[0]) for p in all_pts]
        py = [float(p[1]) for p in all_pts]
        x1, x2 = min(px), max(px)
        y1, y2 = min(py), max(py)
    else:
        x1, x2 = min(xs), max(xs)
        y1, y2 = min(ys), max(ys)
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    area = max(1.0, (x2 - x1) * (y2 - y1))

    # Asse presa / yaw nel piano immagine: usare il bordo reale del tag (OpenCV: corneri in senso orario
    # da alto-sinistra). Prima era una retta orizzontale sul centro → tag_planar_yaw sempre ~0° anche
    # con scatola inclinata nella camera polso.
    use_aabb_axis = os.environ.get("GO2_GRASP_TAG_AXIS_USE_AABB", "0").lower() in {"1", "true", "yes"}
    grip_axis_px: list[list[float]]
    orient_id: int | None = None
    if use_aabb_axis:
        grip_axis_px = [[round(x1, 1), round(cy, 1)], [round(x2, 1), round(cy, 1)]]
    else:

        def _tag_size_score(tg: dict[str, Any]) -> float:
            try:
                return float(tg.get("mean_edge_px") or 0.0)
            except (TypeError, ValueError):
                return 0.0

        orient_t = max(box_tags, key=_tag_size_score)
        orient_id = int(orient_t.get("id", -1))
        c4 = orient_t.get("corners_px") or []
        if len(c4) >= 2:
            p0 = [float(c4[0][0]), float(c4[0][1])]
            p1 = [float(c4[1][0]), float(c4[1][1])]
            ex = p1[0] - p0[0]
            ey = p1[1] - p0[1]
            elen = float(math.hypot(ex, ey))
            if elen < 1e-3:
                grip_axis_px = [[round(x1, 1), round(cy, 1)], [round(x2, 1), round(cy, 1)]]
            else:
                ux, uy = ex / elen, ey / elen
                half = max(8.0, 0.55 * elen, float(orient_t.get("mean_edge_px") or elen) * 0.65)
                ax0 = cx - ux * half
                ay0 = cy - uy * half
                ax1 = cx + ux * half
                ay1 = cy + uy * half
                grip_axis_px = [[round(ax0, 1), round(ay0, 1)], [round(ax1, 1), round(ay1, 1)]]
        else:
            grip_axis_px = [[round(x1, 1), round(cy, 1)], [round(x2, 1), round(cy, 1)]]

    out = {
        "ok": True,
        "source": "apriltag",
        "confidence": 0.98 if len(box_tags) >= 2 else 0.86,
        "box_tag_ids": [int(t.get("id", -1)) for t in box_tags],
        "grip_center_px": [round(cx, 1), round(cy, 1)],
        "grip_axis_px": grip_axis_px,
        "box_bbox_px": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
        "box_size_px": [round(x2 - x1, 1), round(y2 - y1, 1)],
        "box_area_px": round(area, 1),
        "gripper_model": "east_west_close_to_center",
    }
    if orient_id is not None and not use_aabb_axis:
        out["tag_orientation_id"] = orient_id
    if frame_shape is not None and len(frame_shape) >= 2:
        h, w = float(frame_shape[0]), float(frame_shape[1])
        out["approach_error_px"] = [round(cx - w / 2.0, 1), round(cy - h / 2.0, 1)]
        out["approach_error_norm"] = [
            round((cx - w / 2.0) / max(w / 2.0, 1.0), 4),
            round((cy - h / 2.0) / max(h / 2.0, 1.0), 4),
        ]
    return out


def target_base_from_object_detection(
    detection: dict[str, Any],
    frame: np.ndarray,
    *,
    logical_camera_device: int | None = None,
) -> dict[str, Any]:
    if not detection.get("ok"):
        return {"ok": False, "error": detection.get("reason", "no object detection")}
    cam = CameraModel.from_frame(frame)
    bbox = detection.get("bbox_xyxy") or []
    center = detection.get("bbox_center_px") or detection.get("grip_center_px")
    if len(bbox) < 4 or not center:
        return {"ok": False, "error": "detection missing bbox/center"}
    bw = max(1.0, float(bbox[2]) - float(bbox[0]))
    # Approximate monocular depth from apparent box width. This is deliberately
    # conservative and only used when AprilTags are absent.
    box_w_m = float(os.environ.get("GO2_BOX_APPROX_WIDTH_M", "0.16"))
    depth = (box_w_m * cam.fx) / bw
    depth = float(np.clip(depth, 0.20, 0.70))
    cx_px, cy_px = float(center[0]), float(center[1])
    lat = ((cx_px - cam.cx) / max(cam.fx, 1.0)) * depth
    if os.environ.get("GO2_BOX_TARGET_NEGATE_LATERAL", "0").lower() in {"1", "true", "yes"}:
        lat = -lat
    z = DEFAULT_CAMERA_HEIGHT_M - ((cy_px - cam.cy) / max(cam.fy, 1.0)) * depth - float(
        os.environ.get("GO2_BOX_TARGET_HEIGHT_OFFSET_M", "0.10")
    )
    base_xyz = [
        float(np.clip(depth, 0.18, 0.72)),
        float(np.clip(lat, -0.35, 0.35)),
        float(np.clip(z, 0.04, 0.24)),
    ]
    cam_off = camera_device_preview_offset_m(logical_camera_device)
    base_xyz = [base_xyz[0] + cam_off[0], base_xyz[1] + cam_off[1], base_xyz[2] + cam_off[2]]
    return {
        "ok": True,
        "base_xyz_m": [
            round(base_xyz[0], 4),
            round(base_xyz[1], 4),
            round(base_xyz[2], 4),
        ],
        "calibration": "monocular bbox heuristic; AprilTag/RealSense depth preferred when available; optional per-camera preview offset supported",
        "source": detection.get("backend", "object_detection"),
        "bbox_width_px": round(bw, 2),
        "assumed_box_width_m": box_w_m,
        "logical_camera_device": logical_camera_device,
    }


def grip_point_from_object_detection(detection: dict[str, Any], frame_shape: tuple[int, int]) -> dict[str, Any]:
    if not detection.get("ok"):
        return {"ok": False, "source": "object_detection", "reason": detection.get("reason", "no detection")}
    h, w = float(frame_shape[0]), float(frame_shape[1])
    center = detection.get("grip_center_px") or detection.get("bbox_center_px")
    bbox = detection.get("bbox_xyxy") or []
    if not center or len(bbox) < 4:
        return {"ok": False, "source": "object_detection", "reason": "missing bbox/center"}
    cx, cy = float(center[0]), float(center[1])
    x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
    area = max(1.0, (x2 - x1) * (y2 - y1))
    return {
        "ok": True,
        "source": detection.get("backend", "object_detection"),
        "confidence": float(detection.get("confidence") or 0.0),
        "grip_center_px": [round(cx, 1), round(cy, 1)],
        "grip_axis_px": detection.get("grip_axis_px") or [[round(x1, 1), round(cy, 1)], [round(x2, 1), round(cy, 1)]],
        "box_bbox_px": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
        "box_size_px": [round(x2 - x1, 1), round(y2 - y1, 1)],
        "box_area_px": round(area, 1),
        "approach_error_px": [round(cx - w / 2.0, 1), round(cy - h / 2.0, 1)],
        "approach_error_norm": [
            round((cx - w / 2.0) / max(w / 2.0, 1.0), 4),
            round((cy - h / 2.0) / max(h / 2.0, 1.0), 4),
        ],
        "gripper_model": "east_west_close_to_center",
    }


def merge_grip_point(
    tag_grip: dict[str, Any],
    object_grip: dict[str, Any],
    *,
    prefer_tag_grip: bool = False,
) -> dict[str, Any]:
    """Se ``prefer_tag_grip`` (o env ``GO2_GRASP_PREFER_TAG_GRIP``), usa solo AprilTag quando presente."""
    if prefer_tag_grip and tag_grip.get("ok"):
        out = dict(tag_grip)
        out["source"] = "apriltag_preferred"
        return out
    if tag_grip.get("ok") and object_grip.get("ok"):
        out = dict(tag_grip)
        out["source"] = "apriltag+object_detection"
        out["object_detection"] = object_grip
        out["confidence"] = round(max(float(tag_grip.get("confidence", 0.0)), float(object_grip.get("confidence", 0.0))), 4)
        return out
    if tag_grip.get("ok"):
        return tag_grip
    if object_grip.get("ok"):
        return object_grip
    return {"ok": False, "source": "none", "reason": tag_grip.get("reason") or object_grip.get("reason") or "no grip point"}


def grip_tag_planar_yaw_rad(grip: dict[str, Any]) -> float | None:
    """
    Angolo nel piano immagine dell'asse «largo» box (``grip_axis_px``): coincide con l'orientamento
    del QR/tag sulla faccia scatola. Usato per ruotare pre-approccio / presa nel frame base (~orizzontale).
    """
    ax = grip.get("grip_axis_px")
    if not (isinstance(ax, list) and len(ax) >= 2):
        return None
    a, b = ax[0], ax[1]
    if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and len(a) >= 2 and len(b) >= 2):
        return None
    try:
        return float(
            math.atan2(float(b[1]) - float(a[1]), float(b[0]) - float(a[0]))
        )
    except (TypeError, ValueError):
        return None


def build_grasp_preview(target_base: dict[str, Any]) -> dict[str, Any]:
    if not target_base.get("ok"):
        return {"ok": False, "error": target_base.get("error", "no target")}

    x, y, z = target_base["base_xyz_m"]
    # Offset default: arretramento lungo +X base braccio, poi presa.
    # ``tag_planar_yaw_rad`` è l'angolo di un bordo tag **nel piano immagine** (pixel): non coincide
    # con una rotazione attorno all'asse verticale del **base braccio**. Ruotare (dx,dy) base con ψ
    # immagine sposta la pre‑presa lateralmente (es. ~90° → «si allontana» invece di avvicinarsi).
    # Solo con GO2_GRASP_ORIENT_PREVIEW_IMAGE_AS_BASE_YAW=1 si applica quella ψ (sperimentale).
    use_tag_yaw = os.environ.get("GO2_GRASP_ORIENT_PREVIEW_TO_TAG", "1").lower() in {"1", "true", "yes"}
    image_as_base = os.environ.get("GO2_GRASP_ORIENT_PREVIEW_IMAGE_AS_BASE_YAW", "0").lower() in {
        "1",
        "true",
        "yes",
    }
    psi = math.radians(float(os.environ.get("GO2_GRASP_TAG_YAW_OFFSET_DEG", "0")))
    if use_tag_yaw and image_as_base:
        raw = target_base.get("tag_planar_yaw_rad")
        if raw is not None:
            try:
                psi += float(raw)
            except (TypeError, ValueError):
                pass
    c, s = math.cos(psi), math.sin(psi)

    def rot(dx: float, dy: float) -> tuple[float, float]:
        return c * dx - s * dy, s * dx + c * dy

    d_pre_x, d_pre_y = rot(-0.06, 0.0)
    d_ap_x, d_ap_y = rot(-0.015, 0.0)
    d_li_x, d_li_y = rot(-0.04, 0.0)
    stages = [
        ("pre_grasp", x + d_pre_x, y + d_pre_y, z + 0.12),
        ("approach", x + d_ap_x, y + d_ap_y, z + 0.045),
        ("grasp", x, y, z + 0.02),
        ("lift", x + d_li_x, y + d_li_y, z + 0.18),
    ]
    plan = []
    prev_q: list[float] | None = None
    for name, sx, sy, sz in stages:
        q = ik_reach(sx, sy, sz, primary_seed=prev_q)
        if q is None:
            return {"ok": False, "failed_stage": name, "target_xyz_m": [sx, sy, sz]}
        prev_q = q
        tip = fk_tool_tip(q)
        plan.append(
            {
                "stage": name,
                "target_xyz_m": [round(float(sx), 4), round(float(sy), 4), round(float(sz), 4)],
                "joints_rad": [round(float(v), 4) for v in q],
                "fk_tip_xyz_m": [round(float(v), 4) for v in tip],
            }
        )

    gripper = [
        {"stage": "pre_grasp", "gripper": "open"},
        {"stage": "grasp", "gripper": "close", "hold_s": 0.6},
        {"stage": "lift", "gripper": "hold_closed"},
    ]
    return {"ok": True, "mode": "dry-run", "plan": plan, "gripper": gripper}


def plan_from_frame(
    frame: np.ndarray,
    object_detection: dict[str, Any] | None = None,
    *,
    prefer_tag_grip: bool | None = None,
    logical_camera_device: int | None = None,
    include_tag_ids: frozenset[int] | None = None,
    tag_edge_length_overrides: dict[int, float] | None = None,
) -> dict[str, Any]:
    tags = detect_box_tags(frame, include_tag_ids=include_tag_ids)
    poses = estimate_tag_poses(frame, tags, tag_edge_length_overrides=tag_edge_length_overrides)
    if logical_camera_device is not None:
        for p in poses.get("poses") or []:
            if isinstance(p, dict):
                p["logical_camera_device"] = int(logical_camera_device)
    tag_target = estimate_box_target_base(poses)
    obj_target = target_base_from_object_detection(
        object_detection or {},
        frame,
        logical_camera_device=logical_camera_device,
    ) if object_detection else {"ok": False}
    target = tag_target if tag_target.get("ok") else obj_target
    tag_grip = grip_point_from_tags(tags, frame.shape[:2])
    yaw = grip_tag_planar_yaw_rad(tag_grip)
    if target.get("ok") and yaw is not None:
        target = dict(target)
        target["tag_planar_yaw_rad"] = round(float(yaw), 6)
        target["tag_planar_yaw_deg"] = round(float(math.degrees(yaw)), 3)
    preview = build_grasp_preview(target)
    obj_grip = grip_point_from_object_detection(object_detection or {}, frame.shape[:2]) if object_detection else {"ok": False}
    if prefer_tag_grip is None:
        prefer_tag_grip = os.environ.get("GO2_GRASP_PREFER_TAG_GRIP", "0").lower() in {"1", "true", "yes"}
    grip = merge_grip_point(tag_grip, obj_grip, prefer_tag_grip=bool(prefer_tag_grip))
    return {
        "ok": bool(preview.get("ok")),
        "logical_camera_device": logical_camera_device,
        "tags": tags,
        "poses": poses,
        "target": target,
        "target_sources": {"apriltag": tag_target, "object_detection": obj_target},
        "object_detection": object_detection or {"ok": False, "backend": "not_run"},
        "grip_point": grip,
        "preview": preview,
        "tag_calibration": {
            "family": "tag25h9",
            "box_tag_edge_m": BOX_TAG_SIZE_M,
            "reference_tag_edge_m": REFERENCE_TAG_SIZE_M,
            "reference_tag_id": REFERENCE_TAG_ID_LIDAR_FRAME,
        },
    }


def draw_tags(frame: np.ndarray, tags_result: dict[str, Any]) -> np.ndarray:
    out = frame.copy()
    for tag in tags_result.get("tags", []):
        pts = np.array(tag["corners_px"], dtype=np.int32)
        tid = int(tag["id"])
        edge_m = tag_edge_length_m(tid)
        mm = int(round(edge_m * 1000.0))
        color = (0, 165, 255) if tid == REFERENCE_TAG_ID_LIDAR_FRAME else (0, 255, 0)
        cv2.polylines(out, [pts], True, color, 2)
        cx, cy = map(int, tag["center_px"])
        cv2.putText(out, f"id {tid} ~{mm}mm", (cx + 6, cy - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 2)
    return out


def draw_grasp_overlay(frame: np.ndarray, plan_result: dict[str, Any]) -> np.ndarray:
    out = draw_tags(frame, (plan_result.get("tags") or {}))
    h, w = out.shape[:2]
    img_center = (int(round(w / 2.0)), int(round(h / 2.0)))
    cv2.drawMarker(out, img_center, (255, 255, 255), cv2.MARKER_CROSS, 22, 1)

    obj = (plan_result.get("object_detection") or {})
    if obj.get("ok"):
        bbox = obj.get("bbox_xyxy") or []
        if len(bbox) >= 4:
            x1, y1, x2, y2 = [int(round(float(v))) for v in bbox[:4]]
            cv2.rectangle(out, (x1, y1), (x2, y2), (80, 200, 255), 2)
            label = f"det {obj.get('backend', 'object')} conf {float(obj.get('confidence') or 0.0):.2f}"
            cv2.putText(
                out,
                label,
                (max(4, x1), max(18, y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.48,
                (80, 200, 255),
                2,
            )

    grip = (plan_result.get("grip_point") or {})
    if grip.get("ok"):
        center = grip.get("grip_center_px") or []
        axis = grip.get("grip_axis_px") or []
        if len(axis) >= 2:
            p0 = (int(round(float(axis[0][0]))), int(round(float(axis[0][1]))))
            p1 = (int(round(float(axis[1][0]))), int(round(float(axis[1][1]))))
            cv2.line(out, p0, p1, (255, 200, 0), 2)
        if len(center) >= 2:
            gc = (int(round(float(center[0]))), int(round(float(center[1]))))
            cv2.circle(out, gc, 7, (0, 255, 255), 2)
            cv2.line(out, img_center, gc, (0, 255, 255), 1)
            err = grip.get("approach_error_px") or []
            msg = f"grip {gc[0]},{gc[1]}"
            if len(err) >= 2:
                msg += f" err {float(err[0]):+.0f},{float(err[1]):+.0f}px"
            cv2.putText(out, msg, (max(4, gc[0] + 8), max(20, gc[1] - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (0, 255, 255), 2)

    preview = (plan_result.get("preview") or {})
    plan = preview.get("plan") or []
    target = (plan_result.get("target") or {})
    source = str((target.get("source") or grip.get("source") or "—"))
    logical_camera_device = plan_result.get("logical_camera_device")
    hud_lines = [
        f"vision: {'OK' if plan_result.get('ok') else 'NO'} | src: {source}",
        f"cam: {logical_camera_device if logical_camera_device is not None else '—'} | tags: {len(((plan_result.get('tags') or {}).get('tags') or []))} | stages: {len(plan)}",
    ]
    if logical_camera_device is not None:
        hud_lines.append(
            "preview: heuristic base estimate"
            + (" + per-camera offset" if any(abs(v) > 1e-9 for v in camera_device_preview_offset_m(logical_camera_device)) else "")
        )
    base_xyz = target.get("base_xyz_m") or []
    if len(base_xyz) >= 3:
        hud_lines.append(
            f"target xyz: {float(base_xyz[0]):.3f}, {float(base_xyz[1]):.3f}, {float(base_xyz[2]):.3f} m"
        )
    y0 = 22
    for idx, line in enumerate(hud_lines):
        yy = y0 + idx * 18
        cv2.putText(out, line, (10, yy), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (230, 236, 245), 3)
        cv2.putText(out, line, (10, yy), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (15, 23, 42), 1)

    if plan:
        rail_left = 14
        rail_right = max(rail_left + 32, w - 14)
        rail_y = h - 28
        cv2.line(out, (rail_left, rail_y), (rail_right, rail_y), (71, 85, 105), 2)
        names = [str(step.get("stage") or f"s{i + 1}") for i, step in enumerate(plan)]
        denom = max(1, len(names) - 1)
        for idx, step_name in enumerate(names):
            x = int(round(rail_left + (rail_right - rail_left) * (idx / denom))) if len(names) > 1 else int(round((rail_left + rail_right) / 2.0))
            color = (16, 185, 129) if step_name == "grasp" else ((245, 158, 11) if step_name == "approach" else (147, 197, 253))
            cv2.circle(out, (x, rail_y), 6, color, -1)
            cv2.putText(out, step_name, (max(4, x - 22), rail_y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.38, color, 2)
            step_xyz = (plan[idx].get("target_xyz_m") or []) if idx < len(plan) else []
            if len(step_xyz) >= 3:
                xyz_msg = f"{float(step_xyz[0]):.2f}/{float(step_xyz[1]):.2f}/{float(step_xyz[2]):.2f}"
                cv2.putText(out, xyz_msg, (max(4, x - 24), min(h - 6, rail_y + 18)), cv2.FONT_HERSHEY_SIMPLEX, 0.33, (203, 213, 225), 1)
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

"""Planner geometrico 6D per scatole su pavimento, adatto alla Jetson NX.

Dipendenze intenzionalmente limitate a NumPy/OpenCV. Il modulo pianifica e
valida; l'invio DDS resta nei servizi D1 esistenti.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import time
from typing import Any, Iterable

import numpy as np

from go2_dashboard.d1_jog import service
from go2_dashboard.paths import PROJECT_ROOT, ensure_d1_scripts_on_sys_path

ensure_d1_scripts_on_sys_path()
import arm_kinematics_d1_template as kin  # noqa: E402


CALIBRATION_PATH = Path(
    os.environ.get("D1_GRASP6D_CALIBRATION", str(PROJECT_ROOT / "data" / "d1_grasp6d_calibration.json"))
)
HAND_EYE_SAMPLES_PATH = Path(
    os.environ.get("D1_GRASP6D_HAND_EYE_SAMPLES", str(PROJECT_ROOT / "data" / "d1_grasp6d_handeye_samples.json"))
)


def _transform(R: np.ndarray, t: Iterable[float]) -> np.ndarray:
    T = np.eye(4, dtype=float)
    T[:3, :3] = np.asarray(R, dtype=float).reshape(3, 3)
    T[:3, 3] = np.asarray(list(t), dtype=float).reshape(3)
    return T


def _project_rotation(R: np.ndarray) -> np.ndarray:
    u, _, vt = np.linalg.svd(np.asarray(R, dtype=float).reshape(3, 3))
    out = u @ vt
    if np.linalg.det(out) < 0:
        u[:, -1] *= -1
        out = u @ vt
    return out


def _rotation_vector(R: np.ndarray) -> np.ndarray:
    R = _project_rotation(R)
    cos_angle = float(np.clip((np.trace(R) - 1.0) * 0.5, -1.0, 1.0))
    angle = math.acos(cos_angle)
    if angle < 1e-8:
        return np.zeros(3, dtype=float)
    axis = np.array(
        [R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]], dtype=float
    )
    denom = 2.0 * math.sin(angle)
    if abs(denom) < 1e-7:
        vals, vecs = np.linalg.eigh((R + np.eye(3)) * 0.5)
        axis = vecs[:, int(np.argmax(vals))]
    else:
        axis /= denom
    n = float(np.linalg.norm(axis))
    return axis / max(n, 1e-12) * angle


def fk_tool_transform(q_rad: Iterable[float]) -> np.ndarray:
    q = np.asarray(list(q_rad), dtype=float).reshape(6)
    pos, R = kin.fk_full(q)
    tip = pos + R @ np.asarray(kin.TOOL_TIP_OFFSET, dtype=float)
    return _transform(R, tip)


def _pose_error(target: np.ndarray, current: np.ndarray) -> np.ndarray:
    return np.concatenate(
        [
            target[:3, 3] - current[:3, 3],
            _rotation_vector(target[:3, :3] @ current[:3, :3].T),
        ]
    )


def _numeric_pose_jacobian(q: np.ndarray, eps: float = 1e-5) -> np.ndarray:
    base = fk_tool_transform(q)
    J = np.zeros((6, 6), dtype=float)
    for i in range(6):
        q2 = q.copy()
        lo, hi = kin.J_LIMITS[i]
        q2[i] = min(hi, max(lo, q2[i] + eps))
        if abs(q2[i] - q[i]) < 1e-12:
            q2[i] = min(hi, max(lo, q2[i] - eps))
        dq = q2[i] - q[i]
        if abs(dq) < 1e-12:
            continue
        nxt = fk_tool_transform(q2)
        J[:3, i] = (nxt[:3, 3] - base[:3, 3]) / dq
        J[3:, i] = _rotation_vector(nxt[:3, :3] @ base[:3, :3].T) / dq
    return J


def ik_pose(
    target: np.ndarray,
    *,
    primary_seed: Iterable[float] | None = None,
    max_iterations: int = 220,
) -> dict[str, Any]:
    """IK SE(3) DLS: XYZ in metri e orientamento completo del TCP."""
    target = np.asarray(target, dtype=float).reshape(4, 4)
    seeds: list[np.ndarray] = []
    if primary_seed is not None:
        seed = np.asarray(list(primary_seed), dtype=float)
        if seed.size >= 6:
            seeds.append(seed[:6])
    seeds.extend(
        np.asarray(s, dtype=float)
        for s in [
            kin.ARM_FOLD_POSE,
            [0.0, -1.2, 0.8, 0.0, 0.4, 0.0],
            [0.6, -1.0, 0.7, 0.0, 0.4, 0.0],
            [-0.6, -1.0, 0.7, 0.0, -0.4, 0.0],
        ]
    )
    pos_tol = float(os.environ.get("D1_GRASP6D_IK_POS_TOL_M", "0.008"))
    rot_tol = math.radians(float(os.environ.get("D1_GRASP6D_IK_ROT_TOL_DEG", "5")))
    damping = float(os.environ.get("D1_GRASP6D_IK_DAMPING", "0.008"))
    orientation_weight = float(os.environ.get("D1_GRASP6D_IK_ORIENT_WEIGHT", "0.35"))
    best: dict[str, Any] | None = None
    for raw_seed in seeds:
        q = kin._clamp_q(raw_seed)
        for _ in range(max_iterations):
            cur = fk_tool_transform(q)
            err = _pose_error(target, cur)
            pos_err = float(np.linalg.norm(err[:3]))
            rot_err = float(np.linalg.norm(err[3:]))
            if pos_err <= pos_tol and rot_err <= rot_tol:
                break
            weighted = err.copy()
            weighted[3:] *= orientation_weight
            J = _numeric_pose_jacobian(q)
            J[3:, :] *= orientation_weight
            try:
                dq = J.T @ np.linalg.solve(J @ J.T + damping * np.eye(6), weighted)
            except np.linalg.LinAlgError:
                break
            n = float(np.linalg.norm(dq))
            if n > 0.12:
                dq *= 0.12 / n
            q = kin._clamp_q(q + dq)
        cur = fk_tool_transform(q)
        err = _pose_error(target, cur)
        result = {
            "q_rad": q.tolist(),
            "servo_deg": service.clamp_servo_deg(np.degrees(q).tolist() + [50.0]),
            "position_error_m": float(np.linalg.norm(err[:3])),
            "rotation_error_deg": math.degrees(float(np.linalg.norm(err[3:]))),
        }
        score = result["position_error_m"] + math.radians(result["rotation_error_deg"]) * 0.03
        if best is None or score < best["score"]:
            best = {**result, "score": score}
    if best is None:
        return {"ok": False, "reason": "ik_no_solution"}
    ok = best["position_error_m"] <= pos_tol and best["rotation_error_deg"] <= math.degrees(rot_tol)
    best["ok"] = ok
    if not ok:
        best["reason"] = "ik_residual_too_high"
    best.pop("score", None)
    return best


def depth_to_points(depth_m: np.ndarray, intrinsics: dict[str, Any], *, stride: int = 3) -> tuple[np.ndarray, np.ndarray]:
    depth = np.asarray(depth_m, dtype=np.float32)
    ys, xs = np.mgrid[0 : depth.shape[0] : stride, 0 : depth.shape[1] : stride]
    z = depth[::stride, ::stride]
    valid = np.isfinite(z) & (z > 0.12) & (z < float(os.environ.get("D1_GRASP6D_MAX_DEPTH_M", "1.2")))
    x = (xs.astype(float) - float(intrinsics["ppx"])) * z / float(intrinsics["fx"])
    y = (ys.astype(float) - float(intrinsics["ppy"])) * z / float(intrinsics["fy"])
    points = np.stack([x, y, z], axis=-1)
    return points[valid], np.stack([ys, xs], axis=-1)[valid]


def estimate_plane_ransac(points: np.ndarray, *, iterations: int = 140) -> dict[str, Any]:
    pts = np.asarray(points, dtype=float).reshape(-1, 3)
    if len(pts) < 150:
        return {"ok": False, "reason": "insufficient_depth_points"}
    rng = np.random.default_rng(7)
    threshold = float(os.environ.get("D1_GRASP6D_PLANE_THRESHOLD_M", "0.012"))
    best_count = 0
    best_normal = None
    best_d = None
    for _ in range(iterations):
        a, b, c = pts[rng.choice(len(pts), 3, replace=False)]
        normal = np.cross(b - a, c - a)
        n = float(np.linalg.norm(normal))
        if n < 1e-8:
            continue
        normal /= n
        d = -float(np.dot(normal, a))
        dist = np.abs(pts @ normal + d)
        count = int(np.count_nonzero(dist < threshold))
        if count > best_count:
            best_count, best_normal, best_d = count, normal.copy(), d
    if best_normal is None or best_count < max(100, int(len(pts) * 0.18)):
        return {"ok": False, "reason": "floor_plane_not_found", "inliers": best_count}
    if best_normal[1] > 0:  # RealSense Y points down: floor normal should point roughly upward.
        best_normal *= -1
        best_d = -float(best_d)
    return {
        "ok": True,
        "normal": best_normal,
        "d": float(best_d),
        "inlier_count": best_count,
        "inlier_fraction": round(best_count / len(pts), 4),
        "threshold_m": threshold,
    }


def estimate_box_pose(depth_m: np.ndarray, intrinsics: dict[str, Any]) -> dict[str, Any]:
    points, pixels = depth_to_points(depth_m, intrinsics)
    plane = estimate_plane_ransac(points)
    if not plane.get("ok"):
        return {"ok": False, "reason": plane.get("reason"), "plane": plane}
    normal = np.asarray(plane["normal"], dtype=float)
    signed = points @ normal + float(plane["d"])
    min_h = float(os.environ.get("D1_GRASP6D_MIN_BOX_HEIGHT_M", "0.025"))
    max_h = float(os.environ.get("D1_GRASP6D_MAX_BOX_HEIGHT_M", "0.45"))
    object_mask = (signed > min_h) & (signed < max_h)
    obj = points[object_mask]
    obj_pixels = pixels[object_mask]
    if len(obj) < 80:
        return {"ok": False, "reason": "no_cluster_above_floor", "point_count": int(len(obj)), "plane": plane}

    # Mantiene il componente 2D maggiore: economico sulla NX e robusto per una scatola isolata.
    import cv2

    stride = 3
    mask = np.zeros((depth_m.shape[0] // stride + 1, depth_m.shape[1] // stride + 1), dtype=np.uint8)
    mask[(obj_pixels[:, 0] // stride).astype(int), (obj_pixels[:, 1] // stride).astype(int)] = 255
    # La D456 restituisce spesso buchi sui cartoni stampati/lucidi. Colleghiamo
    # solo vicini locali nella maschera sottocampionata, senza inventare depth.
    kernel = np.ones((3, 3), dtype=np.uint8)
    connected_mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    connected_mask = cv2.dilate(connected_mask, kernel, iterations=1)
    nlabels, labels, stats, _ = cv2.connectedComponentsWithStats(connected_mask, 8)
    if nlabels <= 1:
        return {"ok": False, "reason": "object_component_not_found", "plane": plane}
    point_labels = labels[(obj_pixels[:, 0] // stride).astype(int), (obj_pixels[:, 1] // stride).astype(int)]
    min_cluster_points = int(os.environ.get("D1_GRASP6D_MIN_CLUSTER_POINTS", "35"))
    components: list[dict[str, Any]] = []
    image_center = np.array(
        [float(intrinsics.get("ppy", depth_m.shape[0] * 0.5)), float(intrinsics.get("ppx", depth_m.shape[1] * 0.5))]
    )
    image_diag = math.hypot(depth_m.shape[0], depth_m.shape[1])
    for candidate_label in range(1, nlabels):
        candidate_keep = point_labels == candidate_label
        count = int(np.count_nonzero(candidate_keep))
        if count <= 0:
            continue
        center_px = np.mean(obj_pixels[candidate_keep], axis=0)
        center_distance = float(np.linalg.norm(center_px - image_center) / max(image_diag, 1.0))
        components.append(
            {
                "label": candidate_label,
                "point_count": count,
                "mask_area": int(stats[candidate_label, cv2.CC_STAT_AREA]),
                "center_px_yx": center_px.tolist(),
                "center_distance_norm": center_distance,
            }
        )
    eligible = [c for c in components if int(c["point_count"]) >= min_cluster_points]
    if not eligible:
        best_count = max((int(c["point_count"]) for c in components), default=0)
        return {
            "ok": False,
            "reason": "object_cluster_too_small",
            "point_count": best_count,
            "points_above_floor": int(len(obj)),
            "min_cluster_points": min_cluster_points,
            "components": components,
        }
    # La posa Search porta intenzionalmente l'oggetto target vicino al centro.
    # La dimensione viene validata subito dopo, quindi una valigia laterale non
    # prevale sulla scatola centrale solo perche' contiene più pixel depth.
    selected_component = min(
        eligible,
        key=lambda c: float(c["center_distance_norm"]) - min(int(c["point_count"]), 200) * 0.00015,
    )
    label = int(selected_component["label"])
    keep = point_labels == label
    cluster = obj[keep]

    center_observed = np.median(cluster, axis=0)
    cov = np.cov((cluster - center_observed).T)
    vals, vecs = np.linalg.eigh(cov)
    order = np.argsort(vals)[::-1]
    axes = vecs[:, order]
    # L'asse verticale e' quello piu' allineato alla normale del pavimento.
    vertical_i = int(np.argmax(np.abs(axes.T @ normal)))
    vertical = axes[:, vertical_i]
    if np.dot(vertical, normal) < 0:
        vertical *= -1
    horizontal = [axes[:, i] for i in range(3) if i != vertical_i]
    h0 = horizontal[0] - vertical * float(np.dot(horizontal[0], vertical))
    h0 /= max(float(np.linalg.norm(h0)), 1e-12)
    h1 = np.cross(vertical, h0)
    h1 /= max(float(np.linalg.norm(h1)), 1e-12)
    R = np.column_stack([h0, h1, vertical])
    # Le camere vedono spesso solo top e due facce: l'altezza non va stimata
    # dallo spessore della superficie osservata, ma dalla distanza dal pavimento.
    floor_height = cluster @ normal + float(plane["d"])
    height = float(np.percentile(floor_height, 96.0))
    floor_projection = cluster - np.outer(floor_height, normal)
    local_h = floor_projection @ R[:, :2]
    low_h = np.percentile(local_h, 2.0, axis=0)
    high_h = np.percentile(local_h, 98.0, axis=0)
    dims = np.array([high_h[0] - low_h[0], high_h[1] - low_h[1], height], dtype=float)
    center_floor = R[:, :2] @ ((low_h + high_h) * 0.5)
    # La componente lungo la normale del piano e' -d.
    center = center_floor + normal * (-float(plane["d"]) + height * 0.5)
    min_dim = float(os.environ.get("D1_GRASP6D_MIN_BOX_DIM_M", "0.025"))
    max_dim = float(os.environ.get("D1_GRASP6D_MAX_BOX_DIM_M", "0.45"))
    if np.any(dims < min_dim) or np.any(dims > max_dim):
        return {
            "ok": False,
            "reason": "box_dimensions_out_of_range",
            "dimensions_m": dims.tolist(),
            "plane": plane,
        }
    return {
        "ok": True,
        "T_camera_box": _transform(R, center),
        "center_camera_m": center.tolist(),
        "rotation_camera": R.tolist(),
        "dimensions_m": dims.tolist(),
        "point_count": int(len(cluster)),
        "selected_component": selected_component,
        "components": components,
        "plane": {**plane, "normal": normal.tolist()},
    }


def load_calibration() -> dict[str, Any]:
    try:
        data = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "reason": "handeye_calibration_missing", "path": str(CALIBRATION_PATH)}
    T = data.get("T_tool_camera")
    if not isinstance(T, list) or len(T) != 4:
        return {"ok": False, "reason": "handeye_calibration_invalid", "path": str(CALIBRATION_PATH)}
    return {"ok": True, **data, "T_tool_camera_np": np.asarray(T, dtype=float)}


def build_handeye_calibration(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Costruisce camera->tool da pose FK e pose board->camera osservate."""
    if len(samples) < 8:
        return {"ok": False, "reason": "at_least_8_samples_required", "sample_count": len(samples)}
    import cv2

    R_g2b: list[np.ndarray] = []
    t_g2b: list[np.ndarray] = []
    R_t2c: list[np.ndarray] = []
    t_t2c: list[np.ndarray] = []
    for sample in samples:
        Tg = np.asarray(sample.get("T_base_tool"), dtype=float)
        Tc = np.asarray(sample.get("T_camera_target"), dtype=float)
        if Tg.shape != (4, 4) or Tc.shape != (4, 4):
            return {"ok": False, "reason": "invalid_sample_transform"}
        R_g2b.append(Tg[:3, :3])
        t_g2b.append(Tg[:3, 3])
        R_t2c.append(Tc[:3, :3])
        t_t2c.append(Tc[:3, 3])
    R_c2g, t_c2g = cv2.calibrateHandEye(R_g2b, t_g2b, R_t2c, t_t2c, method=cv2.CALIB_HAND_EYE_TSAI)
    T_tool_camera = _transform(R_c2g, np.asarray(t_c2g).reshape(3))
    base_targets = [np.asarray(s["T_base_tool"]) @ T_tool_camera @ np.asarray(s["T_camera_target"]) for s in samples]
    centers = np.stack([T[:3, 3] for T in base_targets])
    center_mean = np.mean(centers, axis=0)
    trans_rms = float(np.sqrt(np.mean(np.sum((centers - center_mean) ** 2, axis=1))))
    R_ref = _project_rotation(sum(T[:3, :3] for T in base_targets))
    rot_errors = [math.degrees(float(np.linalg.norm(_rotation_vector(T[:3, :3] @ R_ref.T)))) for T in base_targets]
    rot_rms = float(np.sqrt(np.mean(np.square(rot_errors))))
    max_trans = float(os.environ.get("D1_GRASP6D_CALIB_MAX_RMS_M", "0.010"))
    max_rot = float(os.environ.get("D1_GRASP6D_CALIB_MAX_RMS_DEG", "3.0"))
    record = {
        "version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sample_count": len(samples),
        "T_tool_camera": T_tool_camera.tolist(),
        "translation_rms_m": trans_rms,
        "rotation_rms_deg": rot_rms,
        "valid": bool(trans_rms <= max_trans and rot_rms <= max_rot),
    }
    if not record["valid"]:
        return {"ok": False, "reason": "handeye_residual_too_high", **record}
    CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CALIBRATION_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    tmp.replace(CALIBRATION_PATH)
    return {"ok": True, **record, "path": str(CALIBRATION_PATH)}


def detect_calibration_marker(color_bgr: np.ndarray, intrinsics: dict[str, Any]) -> dict[str, Any]:
    """Pose target ArUco o scacchiera nel frame ottico camera."""
    import cv2

    marker_id = int(os.environ.get("D1_GRASP6D_CALIB_MARKER_ID", "0"))
    marker_size = float(os.environ.get("D1_GRASP6D_CALIB_MARKER_SIZE_M", "0.060"))
    K = np.array(
        [
            [float(intrinsics["fx"]), 0.0, float(intrinsics["ppx"])],
            [0.0, float(intrinsics["fy"]), float(intrinsics["ppy"])],
            [0.0, 0.0, 1.0],
        ],
        dtype=float,
    )
    dist = np.asarray(intrinsics.get("coeffs") or [0.0] * 5, dtype=float)
    if hasattr(cv2, "aruco"):
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        corners, ids, _ = cv2.aruco.detectMarkers(color_bgr, dictionary)
        flat_ids = [] if ids is None else [int(x) for x in ids.reshape(-1)]
        if marker_id in flat_ids:
            index = flat_ids.index(marker_id)
            half = marker_size * 0.5
            object_points = np.array(
                [[-half, half, 0.0], [half, half, 0.0], [half, -half, 0.0], [-half, -half, 0.0]],
                dtype=np.float32,
            )
            image_points = np.asarray(corners[index], dtype=np.float32).reshape(4, 2)
            ok, rvec, tvec = cv2.solvePnP(
                object_points, image_points, K, dist, flags=cv2.SOLVEPNP_IPPE_SQUARE
            )
            if ok:
                R, _ = cv2.Rodrigues(rvec)
                return {
                    "ok": True,
                    "target_type": "aruco_4x4_50",
                    "marker_id": marker_id,
                    "marker_size_m": marker_size,
                    "T_camera_target": _transform(R, np.asarray(tvec).reshape(3)).tolist(),
                    "corners_px": image_points.tolist(),
                }

    cols = int(os.environ.get("D1_GRASP6D_CHESSBOARD_COLS", "7"))
    rows = int(os.environ.get("D1_GRASP6D_CHESSBOARD_ROWS", "5"))
    square = float(os.environ.get("D1_GRASP6D_CHESSBOARD_SQUARE_M", "0.025"))
    gray = cv2.cvtColor(color_bgr, cv2.COLOR_BGR2GRAY)
    pattern = (cols, rows)
    if hasattr(cv2, "findChessboardCornersSB"):
        found, image_points = cv2.findChessboardCornersSB(gray, pattern)
    else:
        found, image_points = cv2.findChessboardCorners(gray, pattern)
    if not found or image_points is None:
        return {"ok": False, "reason": "calibration_target_not_found", "marker_id": marker_id}
    object_points = np.zeros((rows * cols, 3), dtype=np.float32)
    object_points[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * square
    image_points = np.asarray(image_points, dtype=np.float32).reshape(-1, 2)
    ok, rvec, tvec = cv2.solvePnP(object_points, image_points, K, dist)
    if not ok:
        return {"ok": False, "reason": "calibration_target_pnp_failed"}
    R, _ = cv2.Rodrigues(rvec)
    return {
        "ok": True,
        "target_type": "chessboard",
        "pattern_inner_corners": [cols, rows],
        "square_size_m": square,
        "T_camera_target": _transform(R, np.asarray(tvec).reshape(3)).tolist(),
        "corners_px": image_points.tolist(),
    }


def list_handeye_samples() -> list[dict[str, Any]]:
    try:
        data = json.loads(HAND_EYE_SAMPLES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def append_handeye_sample(T_base_tool: np.ndarray, T_camera_target: np.ndarray) -> dict[str, Any]:
    samples = list_handeye_samples()
    samples.append(
        {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "T_base_tool": np.asarray(T_base_tool, dtype=float).reshape(4, 4).tolist(),
            "T_camera_target": np.asarray(T_camera_target, dtype=float).reshape(4, 4).tolist(),
        }
    )
    HAND_EYE_SAMPLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = HAND_EYE_SAMPLES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(samples, indent=2), encoding="utf-8")
    tmp.replace(HAND_EYE_SAMPLES_PATH)
    return {"ok": True, "sample_count": len(samples), "path": str(HAND_EYE_SAMPLES_PATH)}


def _candidate_orientation(vertical: np.ndarray, closing: np.ndarray) -> np.ndarray:
    x_axis = -vertical / max(float(np.linalg.norm(vertical)), 1e-12)  # tool +X approccia verso il basso
    y_axis = closing - x_axis * float(np.dot(closing, x_axis))
    y_axis /= max(float(np.linalg.norm(y_axis)), 1e-12)
    z_axis = np.cross(x_axis, y_axis)
    z_axis /= max(float(np.linalg.norm(z_axis)), 1e-12)
    return _project_rotation(np.column_stack([x_axis, y_axis, z_axis]))


def _floor_clearance_ok(q_rad: Iterable[float]) -> bool:
    floor_z = float(os.environ.get("D1_GRASP6D_FLOOR_Z_ARM_M", "0.0"))
    margin = float(os.environ.get("D1_GRASP6D_LINK_FLOOR_MARGIN_M", "0.015"))
    pts = np.asarray(kin.fk_chain_positions(list(q_rad)), dtype=float)
    # Il mount e i primi link sono sul robot; controlliamo la parte distale.
    return bool(np.all(pts[3:, 2] >= floor_z + margin))


def _trajectory_collision_free(
    q_start: Iterable[float],
    q_end: Iterable[float],
    T_base_box: np.ndarray,
    dimensions_m: np.ndarray,
) -> bool:
    """Controllo conservativo pavimento + link prossimali contro OBB scatola."""
    qa = np.asarray(list(q_start), dtype=float)
    qb = np.asarray(list(q_end), dtype=float)
    R = T_base_box[:3, :3]
    center = T_base_box[:3, 3]
    half = np.asarray(dimensions_m, dtype=float) * 0.5 + float(
        os.environ.get("D1_GRASP6D_COLLISION_MARGIN_M", "0.025")
    )
    for alpha in np.linspace(0.0, 1.0, 9):
        q = kin._clamp_q(qa * (1.0 - alpha) + qb * alpha)
        if not _floor_clearance_ok(q):
            return False
        chain = np.asarray(kin.fk_chain_positions(q), dtype=float)
        # Esclude polso/tool: devono raggiungere il punto di contatto.
        for i in range(max(0, len(chain) - 3)):
            a, b = chain[i], chain[i + 1]
            for beta in np.linspace(0.0, 1.0, 5):
                p = a * (1.0 - beta) + b * beta
                local = R.T @ (p - center)
                if bool(np.all(np.abs(local) <= half)):
                    return False
    return True


def plan_grasp(
    box: dict[str, Any],
    *,
    current_servo_deg: list[float],
    calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not box.get("ok"):
        return {"ok": False, "reason": box.get("reason", "box_pose_invalid")}
    cal = calibration or load_calibration()
    if not cal.get("ok"):
        return {"ok": False, "reason": cal.get("reason"), "calibration": cal}
    q_now = np.radians(np.asarray(current_servo_deg[:6], dtype=float))
    T_base_tool = fk_tool_transform(q_now)
    T_base_box = T_base_tool @ np.asarray(cal["T_tool_camera_np"], dtype=float) @ np.asarray(box["T_camera_box"])
    Rb = T_base_box[:3, :3]
    dims = np.asarray(box["dimensions_m"], dtype=float)
    vertical = Rb[:, 2]
    horizontal_dims = dims[:2]
    aperture = float(os.environ.get("D1_GRIPPER_MAX_APERTURE_M", "0.085"))
    pregrasp_m = float(os.environ.get("D1_GRASP6D_PREGRASP_M", "0.10"))
    candidates: list[dict[str, Any]] = []
    for axis_index in np.argsort(horizontal_dims):
        width = float(horizontal_dims[axis_index])
        if width > aperture:
            continue
        closing = Rb[:, int(axis_index)]
        for sign in (1.0, -1.0):
            R_tool = _candidate_orientation(vertical, closing * sign)
            contact = T_base_box[:3, 3] + vertical * float(dims[2] * 0.20)
            grasp_T = _transform(R_tool, contact)
            pre_T = grasp_T.copy()
            pre_T[:3, 3] -= R_tool[:, 0] * pregrasp_m
            pre_ik = ik_pose(pre_T, primary_seed=q_now)
            if not pre_ik.get("ok"):
                continue
            grasp_ik = ik_pose(grasp_T, primary_seed=pre_ik["q_rad"])
            if not grasp_ik.get("ok") or not _trajectory_collision_free(
                pre_ik["q_rad"], grasp_ik["q_rad"], T_base_box, dims
            ):
                continue
            joint_motion = float(np.linalg.norm(np.asarray(grasp_ik["q_rad"]) - q_now))
            score = (
                float(pre_ik["position_error_m"])
                + float(grasp_ik["position_error_m"])
                + math.radians(float(grasp_ik["rotation_error_deg"])) * 0.02
                + joint_motion * 0.002
            )
            candidates.append(
                {
                    "score": score,
                    "closing_width_m": width,
                    "closing_axis_index": int(axis_index),
                    "T_base_pregrasp": pre_T.tolist(),
                    "T_base_grasp": grasp_T.tolist(),
                    "pregrasp": pre_ik,
                    "grasp": grasp_ik,
                }
            )
    if not candidates:
        return {
            "ok": False,
            "reason": "no_safe_6d_grasp_candidate",
            "box_dimensions_m": dims.tolist(),
            "gripper_max_aperture_m": aperture,
        }
    candidates.sort(key=lambda item: float(item["score"]))
    best = candidates[0]
    return {
        "ok": True,
        "source": "rgbd_cuboid_6d",
        "T_base_box": T_base_box.tolist(),
        "box_dimensions_m": dims.tolist(),
        "candidate_count": len(candidates),
        "selected": best,
    }

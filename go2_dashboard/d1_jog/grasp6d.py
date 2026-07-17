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
CALIBRATION_HISTORY_PATH = Path(
    os.environ.get(
        "D1_GRASP6D_CALIBRATION_HISTORY",
        str(PROJECT_ROOT / "data" / "d1_grasp6d_calibration_history.json"),
    )
)
TUNING_PATH = Path(
    os.environ.get("D1_GRASP6D_TUNING", str(PROJECT_ROOT / "data" / "d1_grasp6d_tuning.json"))
)

_TUNING_DEFAULTS: dict[str, float] = {
    "min_box_height_m": 0.025,
    "min_box_height_floor_m": 0.008,
    "min_cluster_points": 35.0,
    "min_box_dim_m": 0.025,
}
_TUNING_LIMITS: dict[str, tuple[float, float]] = {
    "min_box_height_m": (0.006, 0.08),
    "min_box_height_floor_m": (0.004, 0.04),
    "min_cluster_points": (8.0, 300.0),
    "min_box_dim_m": (0.008, 0.10),
}


def _depth_stride() -> int:
    raw = (os.environ.get("D1_GRASP6D_DEPTH_STRIDE") or "3").strip()
    try:
        return max(1, min(6, int(raw)))
    except ValueError:
        return 3


def tuning_info() -> dict[str, Any]:
    values = dict(_TUNING_DEFAULTS)
    try:
        raw = json.loads(TUNING_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key in values:
                if key in raw:
                    lo, hi = _TUNING_LIMITS[key]
                    values[key] = min(hi, max(lo, float(raw[key])))
    except (OSError, ValueError, TypeError):
        pass
    return {"ok": True, "values": values, "path": str(TUNING_PATH)}


def update_tuning(changes: dict[str, Any] | None = None, *, reset: bool = False) -> dict[str, Any]:
    values = dict(_TUNING_DEFAULTS) if reset else dict(tuning_info()["values"])
    for key, raw in (changes or {}).items():
        if key not in values:
            continue
        lo, hi = _TUNING_LIMITS[key]
        values[key] = min(hi, max(lo, float(raw)))
    TUNING_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TUNING_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(values, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(TUNING_PATH)
    return {"ok": True, "values": values, "path": str(TUNING_PATH)}


def _tuning_value(key: str, env_name: str) -> float:
    if env_name in os.environ:
        return float(os.environ[env_name])
    return float(tuning_info()["values"][key])


def _transform(R: np.ndarray, t: Iterable[float]) -> np.ndarray:
    T = np.eye(4, dtype=float)
    T[:3, :3] = np.asarray(R, dtype=float).reshape(3, 3)
    T[:3, 3] = np.asarray(list(t), dtype=float).reshape(3)
    return T


def _safe_inv4(T: np.ndarray) -> np.ndarray | None:
    arr = np.asarray(T, dtype=float)
    if arr.shape != (4, 4) or not np.all(np.isfinite(arr)):
        return None
    try:
        inv = np.linalg.inv(arr)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(inv)):
        return None
    return inv


def _project_rotation(R: np.ndarray) -> np.ndarray:
    try:
        u, _, vt = np.linalg.svd(np.asarray(R, dtype=float).reshape(3, 3))
    except np.linalg.LinAlgError:
        return np.eye(3, dtype=float)
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


def depth_to_points(
    depth_m: np.ndarray,
    intrinsics: dict[str, Any],
    *,
    stride: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    stride = _depth_stride() if stride is None else max(1, int(stride))
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
    # Orienta la normale dal pavimento verso l'origine camera. Vale sia con
    # camera obliqua (normale circa -Y) sia con D456 quasi verticale (-Z).
    # Per n·p+d=0, d positivo significa che l'origine è nel semispazio positivo.
    if float(best_d) < 0.0:
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
    stride = _depth_stride()
    points, pixels = depth_to_points(depth_m, intrinsics, stride=stride)
    plane = estimate_plane_ransac(points)
    if not plane.get("ok"):
        return {"ok": False, "reason": plane.get("reason"), "mask_stride": stride, "plane": plane}
    normal = np.asarray(plane["normal"], dtype=float)
    signed = points @ normal + float(plane["d"])
    min_h_default = _tuning_value("min_box_height_m", "D1_GRASP6D_MIN_BOX_HEIGHT_M")
    max_h = float(os.environ.get("D1_GRASP6D_MAX_BOX_HEIGHT_M", "0.45"))
    min_h_floor = _tuning_value("min_box_height_floor_m", "D1_GRASP6D_MIN_BOX_HEIGHT_FLOOR_M")
    min_cluster_points = int(round(_tuning_value("min_cluster_points", "D1_GRASP6D_MIN_CLUSTER_POINTS")))
    minimum_geometry_points = max(8, min_cluster_points)

    # Fallback adattivo: se la depth e' rumorosa o il box e' quasi complanare,
    # abbassiamo la soglia minima invece di fallire subito con no_cluster_above_floor.
    min_h_used = min_h_default
    object_mask = (signed > min_h_used) & (signed < max_h)
    obj = points[object_mask]
    obj_pixels = pixels[object_mask]
    obj_signed = signed[object_mask]
    debug_max_points = int(os.environ.get("D1_GRASP6D_DEBUG_MAX_POINTS", "1400"))

    def _sample_debug(px: np.ndarray, heights: np.ndarray) -> dict[str, Any]:
        if len(px) <= 0:
            return {"sample_px_yx": [], "sample_height_m": []}
        if len(px) > debug_max_points:
            idx = np.linspace(0, len(px) - 1, debug_max_points, dtype=int)
            px = px[idx]
            heights = heights[idx]
        return {
            "sample_px_yx": px.astype(float).tolist(),
            "sample_height_m": heights.astype(float).tolist(),
        }

    if len(obj) < minimum_geometry_points and min_h_floor < min_h_default:
        min_h_used = min_h_floor
        object_mask = (signed > min_h_used) & (signed < max_h)
        obj = points[object_mask]
        obj_pixels = pixels[object_mask]
        obj_signed = signed[object_mask]
    if len(obj) < minimum_geometry_points:
        dbg = _sample_debug(obj_pixels, obj_signed)
        return {
            "ok": False,
            "reason": "no_cluster_above_floor",
            "point_count": int(len(obj)),
            "min_cluster_points": min_cluster_points,
            "height_threshold_m": {"min": min_h_used, "max": max_h, "default_min": min_h_default},
            "mask_stride": stride,
            **dbg,
            "plane": plane,
        }

    # Mantiene il componente 2D maggiore: economico sulla NX e robusto per una scatola isolata.
    import cv2

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
                "mask_bbox_xywh": [
                    int(stats[candidate_label, cv2.CC_STAT_LEFT]),
                    int(stats[candidate_label, cv2.CC_STAT_TOP]),
                    int(stats[candidate_label, cv2.CC_STAT_WIDTH]),
                    int(stats[candidate_label, cv2.CC_STAT_HEIGHT]),
                ],
                "center_px_yx": center_px.tolist(),
                "center_distance_norm": center_distance,
            }
        )
    eligible = [c for c in components if int(c["point_count"]) >= min_cluster_points]
    if not eligible:
        best_count = max((int(c["point_count"]) for c in components), default=0)
        dbg = _sample_debug(obj_pixels, obj_signed)
        return {
            "ok": False,
            "reason": "object_cluster_too_small",
            "point_count": best_count,
            "points_above_floor": int(len(obj)),
            "min_cluster_points": min_cluster_points,
            "height_threshold_m": {"min": min_h_used, "max": max_h, "default_min": min_h_default},
            "mask_stride": stride,
            "mask_shape_hw": [int(mask.shape[0]), int(mask.shape[1])],
            **dbg,
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
    cluster_pixels = obj_pixels[keep]
    cluster_heights = obj_signed[keep]

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
    min_dim = _tuning_value("min_box_dim_m", "D1_GRASP6D_MIN_BOX_DIM_M")
    max_dim = float(os.environ.get("D1_GRASP6D_MAX_BOX_DIM_M", "0.45"))
    horizontal_invalid = bool(np.any(dims[:2] < min_dim) or np.any(dims[:2] > max_dim))
    height_invalid = bool(dims[2] < min_h_floor or dims[2] > max_h)
    if horizontal_invalid or height_invalid:
        dbg = _sample_debug(cluster_pixels, cluster_heights)
        return {
            "ok": False,
            "reason": "box_dimensions_out_of_range",
            "dimensions_m": dims.tolist(),
            "dimension_limits_m": {
                "horizontal_min": min_dim,
                "horizontal_max": max_dim,
                "height_min": min_h_floor,
                "height_max": max_h,
            },
            **dbg,
            "plane": plane,
        }
    dbg = _sample_debug(cluster_pixels, cluster_heights)
    return {
        "ok": True,
        "T_camera_box": _transform(R, center),
        "center_camera_m": center.tolist(),
        "rotation_camera": R.tolist(),
        "dimensions_m": dims.tolist(),
        "point_count": int(len(cluster)),
        "selected_component": selected_component,
        "height_threshold_m": {"min": min_h_used, "max": max_h, "default_min": min_h_default},
        **dbg,
        "mask_stride": stride,
        "mask_shape_hw": [int(mask.shape[0]), int(mask.shape[1])],
        "components": components,
        "plane": {**plane, "normal": normal.tolist()},
    }


def estimate_box_pose_rgb_guided(
    depth_m: np.ndarray,
    intrinsics: dict[str, Any],
    detection: dict[str, Any],
    *,
    plane_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stima cuboide usando RGB per il footprint e depth per piano/distanza.

    Serve per superfici lucide/scure dove la D456 vede il pavimento ma produce
    pochi punti sulla faccia superiore dell'oggetto.
    """
    if not detection.get("ok"):
        return {"ok": False, "reason": "rgb_detection_missing", "rgb_detection": detection}
    stride = _depth_stride()
    points, pixels = depth_to_points(depth_m, intrinsics, stride=stride)
    plane = plane_hint if isinstance(plane_hint, dict) and plane_hint.get("ok") else estimate_plane_ransac(points)
    if not plane.get("ok"):
        return {"ok": False, "reason": plane.get("reason"), "mask_stride": stride, "plane": plane}
    normal = np.asarray(plane["normal"], dtype=float).reshape(3)
    d = float(plane["d"])
    normal /= max(float(np.linalg.norm(normal)), 1e-12)

    import cv2

    h, w = depth_m.shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    obox = detection.get("orient_box_px")
    if isinstance(obox, list) and len(obox) >= 4:
        pts2 = np.asarray(obox[:4], dtype=np.float32).reshape(-1, 2)
        cv2.fillConvexPoly(mask, np.round(pts2).astype(np.int32), 255)
        footprint_px = pts2
    else:
        xyxy = detection.get("bbox_xyxy") or []
        if not isinstance(xyxy, list) or len(xyxy) < 4:
            return {"ok": False, "reason": "rgb_detection_without_bbox", "rgb_detection": detection}
        x1, y1, x2, y2 = [float(v) for v in xyxy[:4]]
        x1i, y1i = max(0, int(x1)), max(0, int(y1))
        x2i, y2i = min(w - 1, int(x2)), min(h - 1, int(y2))
        if x2i <= x1i or y2i <= y1i:
            return {"ok": False, "reason": "rgb_detection_bbox_empty", "rgb_detection": detection}
        mask[y1i : y2i + 1, x1i : x2i + 1] = 255
        footprint_px = np.asarray([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=np.float32)

    py = np.clip(pixels[:, 0].astype(int), 0, h - 1)
    px = np.clip(pixels[:, 1].astype(int), 0, w - 1)
    in_rgb = mask[py, px] > 0
    roi_points = points[in_rgb]
    roi_pixels = pixels[in_rgb]
    if len(roi_points) < 20:
        return {
            "ok": False,
            "reason": "rgb_guided_roi_depth_too_sparse",
            "point_count": int(len(roi_points)),
            "mask_stride": stride,
            "plane": {**plane, "normal": normal.tolist()},
            "rgb_detection": detection,
        }

    z_ref = float(np.median(roi_points[:, 2]))
    if not np.isfinite(z_ref) or z_ref <= 0.12:
        return {
            "ok": False,
            "reason": "rgb_guided_roi_depth_invalid",
            "point_count": int(len(roi_points)),
            "mask_stride": stride,
            "plane": {**plane, "normal": normal.tolist()},
            "rgb_detection": detection,
        }

    def _pixel_at_depth(u: float, v: float, z: float) -> np.ndarray:
        return np.asarray(
            [
                (float(u) - float(intrinsics["ppx"])) * z / float(intrinsics["fx"]),
                (float(v) - float(intrinsics["ppy"])) * z / float(intrinsics["fy"]),
                z,
            ],
            dtype=float,
        )

    footprint_corners = np.asarray([_pixel_at_depth(float(u), float(v), z_ref) for u, v in footprint_px], dtype=float)
    edges = [footprint_corners[(i + 1) % 4] - footprint_corners[i] for i in range(4)]
    edge_lengths = [float(np.linalg.norm(e)) for e in edges]
    long_i = int(np.argmax(edge_lengths))
    h0 = edges[long_i] - normal * float(np.dot(edges[long_i], normal))
    h0 /= max(float(np.linalg.norm(h0)), 1e-12)
    h1 = np.cross(normal, h0)
    h1 /= max(float(np.linalg.norm(h1)), 1e-12)

    long_len = edge_lengths[long_i]
    adjacent = [edge_lengths[(long_i - 1) % 4], edge_lengths[(long_i + 1) % 4]]
    short_len = float(np.median(adjacent))
    dims_xy = np.asarray([long_len, short_len], dtype=float)
    signed = roi_points @ normal + d
    min_h_floor = _tuning_value("min_box_height_floor_m", "D1_GRASP6D_MIN_BOX_HEIGHT_FLOOR_M")
    max_h = float(os.environ.get("D1_GRASP6D_MAX_BOX_HEIGHT_M", "0.45"))
    top = signed[(signed > min_h_floor) & (signed < max_h)]
    if len(top) >= 6:
        height = float(np.percentile(top, 90.0))
        height_source = "roi_depth_percentile"
    else:
        height = float(os.environ.get("D1_GRASP6D_RGB_GUIDED_HEIGHT_M", "0.045"))
        height_source = "assumed_env"
    height = min(max(height, min_h_floor), max_h)

    min_dim = _tuning_value("min_box_dim_m", "D1_GRASP6D_MIN_BOX_DIM_M")
    max_dim = float(os.environ.get("D1_GRASP6D_MAX_BOX_DIM_M", "0.45"))
    if bool(np.any(dims_xy < min_dim) or np.any(dims_xy > max_dim)):
        return {
            "ok": False,
            "reason": "rgb_guided_dimensions_out_of_range",
            "dimensions_m": [float(dims_xy[0]), float(dims_xy[1]), height],
            "dimension_limits_m": {"horizontal_min": min_dim, "horizontal_max": max_dim},
            "point_count": int(len(roi_points)),
            "points_above_floor": int(len(top)),
            "mask_stride": stride,
            "plane": {**plane, "normal": normal.tolist()},
            "rgb_detection": detection,
        }

    center_observed = np.mean(footprint_corners, axis=0)
    center_floor = center_observed - normal * float(np.dot(center_observed, normal) + d)
    center = center_floor + normal * (height * 0.5)
    R = np.column_stack([h0, h1, normal])
    return {
        "ok": True,
        "source": "rgb_guided_depth_sparse",
        "T_camera_box": _transform(R, center),
        "center_camera_m": center.tolist(),
        "rotation_camera": R.tolist(),
        "dimensions_m": [float(dims_xy[0]), float(dims_xy[1]), height],
        "height_source": height_source,
        "reference_depth_m": z_ref,
        "point_count": int(len(roi_points)),
        "points_above_floor": int(len(top)),
        "mask_stride": stride,
        "height_threshold_m": {"min": min_h_floor, "max": max_h},
        "rgb_detection": detection,
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
    out = solve_handeye_calibration(samples)
    if not out.get("ok"):
        return out
    CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CALIBRATION_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
    tmp.replace(CALIBRATION_PATH)
    return {"ok": True, **out, "path": str(CALIBRATION_PATH)}


def calib_min_samples() -> int:
    """Minimo sample per solve hand-eye (6 consente prune con sessione da 8)."""
    try:
        return max(5, min(12, int(os.environ.get("D1_GRASP6D_CALIB_MIN_SAMPLES", "6"))))
    except ValueError:
        return 6


def solve_handeye_calibration(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Calcola hand-eye senza salvare su disco. Non deve mai alzare LinAlgError."""
    try:
        return _solve_handeye_calibration_impl(samples)
    except np.linalg.LinAlgError as exc:
        return {
            "ok": False,
            "reason": "handeye_linalg_error",
            "detail": str(exc),
            "sample_count": len(samples),
        }
    except ValueError as exc:
        return {
            "ok": False,
            "reason": "handeye_solve_value_error",
            "detail": str(exc),
            "sample_count": len(samples),
        }


def _solve_handeye_calibration_impl(samples: list[dict[str, Any]]) -> dict[str, Any]:
    min_n = calib_min_samples()
    if len(samples) < min_n:
        return {
            "ok": False,
            "reason": "at_least_n_samples_required",
            "sample_count": len(samples),
            "min_samples": min_n,
        }
    import cv2

    Tg_rows: list[np.ndarray] = []
    Tc_rows: list[np.ndarray] = []
    for sample in samples:
        Tg = np.asarray(sample.get("T_base_tool"), dtype=float)
        Tc = np.asarray(sample.get("T_camera_target"), dtype=float)
        if Tg.shape != (4, 4) or Tc.shape != (4, 4):
            return {"ok": False, "reason": "invalid_sample_transform"}
        if _safe_inv4(Tg) is None or _safe_inv4(Tc) is None:
            return {"ok": False, "reason": "singular_sample_transform", "sample_count": len(samples)}
        Tg_rows.append(Tg)
        Tc_rows.append(Tc)

    # Soglie lab D1+AprilGrid: 1cm/3° e' spesso irrealistico con orbit limitata.
    max_trans = float(os.environ.get("D1_GRASP6D_CALIB_MAX_RMS_M", "0.025"))
    max_rot = float(os.environ.get("D1_GRASP6D_CALIB_MAX_RMS_DEG", "6.0"))
    methods = [
        ("tsai", cv2.CALIB_HAND_EYE_TSAI),
        ("park", cv2.CALIB_HAND_EYE_PARK),
        ("horaud", cv2.CALIB_HAND_EYE_HORAUD),
        ("andreff", cv2.CALIB_HAND_EYE_ANDREFF),
        ("daniilidis", cv2.CALIB_HAND_EYE_DANIILIDIS),
    ]
    Tg_inv: list[np.ndarray] = []
    Tc_inv: list[np.ndarray] = []
    for Tg, Tc in zip(Tg_rows, Tc_rows):
        g_inv = _safe_inv4(Tg)
        c_inv = _safe_inv4(Tc)
        if g_inv is None or c_inv is None:
            return {
                "ok": False,
                "reason": "singular_sample_transform",
                "sample_count": len(samples),
            }
        Tg_inv.append(g_inv)
        Tc_inv.append(c_inv)
    transform_variants = [
        ("base_tool__camera_target", Tg_rows, Tc_rows),
        ("tool_base__camera_target", Tg_inv, Tc_rows),
        ("base_tool__target_camera", Tg_rows, Tc_inv),
        ("tool_base__target_camera", Tg_inv, Tc_inv),
    ]

    def residual_for_x(T_tool_camera: np.ndarray) -> dict[str, Any]:
        if not np.all(np.isfinite(T_tool_camera)):
            raise ValueError("non_finite_candidate_transform")
        base_targets = [Tg @ T_tool_camera @ Tc for Tg, Tc in zip(Tg_rows, Tc_rows)]
        if not all(np.all(np.isfinite(T)) for T in base_targets):
            raise ValueError("non_finite_base_targets")
        centers = np.stack([T[:3, 3] for T in base_targets])
        center_mean = np.mean(centers, axis=0)
        trans_errors = np.sqrt(np.sum((centers - center_mean) ** 2, axis=1))
        trans_rms = float(np.sqrt(np.mean(np.square(trans_errors))))
        R_ref = _project_rotation(sum(T[:3, :3] for T in base_targets))
        rot_errors = [
            math.degrees(float(np.linalg.norm(_rotation_vector(T[:3, :3] @ R_ref.T)))) for T in base_targets
        ]
        rot_rms = float(np.sqrt(np.mean(np.square(rot_errors))))
        return {
            "T_tool_camera": T_tool_camera,
            "base_target_centers_m": centers.tolist(),
            "sample_translation_errors_m": trans_errors.tolist(),
            "sample_rotation_errors_deg": rot_errors,
            "translation_rms_m": trans_rms,
            "rotation_rms_deg": rot_rms,
            "score": trans_rms / max(max_trans, 1e-6) + rot_rms / max(max_rot, 1e-6),
        }

    candidates: list[dict[str, Any]] = []
    solver_errors: list[str] = []
    for variant_name, g_rows, t_rows in transform_variants:
        R_g = [T[:3, :3] for T in g_rows]
        t_g = [T[:3, 3] for T in g_rows]
        R_t = [T[:3, :3] for T in t_rows]
        t_t = [T[:3, 3] for T in t_rows]
        for method_name, method in methods:
            try:
                R_x, t_x = cv2.calibrateHandEye(R_g, t_g, R_t, t_t, method=method)
            except cv2.error as exc:
                solver_errors.append(f"{variant_name}/{method_name}: {exc}")
                continue
            X = _transform(R_x, np.asarray(t_x).reshape(3))
            for inverted in (False, True):
                if inverted:
                    X_eval = _safe_inv4(X)
                    if X_eval is None:
                        solver_errors.append(f"{variant_name}/{method_name}/inv=True: singular_X")
                        continue
                else:
                    X_eval = X
                try:
                    res = residual_for_x(X_eval)
                except (ValueError, np.linalg.LinAlgError) as exc:
                    solver_errors.append(f"{variant_name}/{method_name}/inv={inverted}: {exc}")
                    continue
                res.update(
                    {
                        "solver_variant": variant_name,
                        "solver_method": method_name,
                        "candidate_inverted": inverted,
                    }
                )
                candidates.append(res)
    if not candidates:
        return {
            "ok": False,
            "reason": "handeye_solver_failed",
            "detail": "; ".join(solver_errors[-4:]),
            "sample_count": len(samples),
        }
    best = min(candidates, key=lambda row: float(row["score"]))
    diagnostic = sorted(candidates, key=lambda row: float(row["score"]))[:8]
    record = {
        "version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sample_count": len(samples),
        "T_tool_camera": np.asarray(best["T_tool_camera"]).tolist(),
        "translation_rms_m": best["translation_rms_m"],
        "rotation_rms_deg": best["rotation_rms_deg"],
        "base_target_centers_m": best["base_target_centers_m"],
        "sample_translation_errors_m": best["sample_translation_errors_m"],
        "sample_rotation_errors_deg": best["sample_rotation_errors_deg"],
        "solver_variant": best["solver_variant"],
        "solver_method": best["solver_method"],
        "candidate_inverted": best["candidate_inverted"],
        "solver_diagnostic": [
            {
                "solver_variant": row["solver_variant"],
                "solver_method": row["solver_method"],
                "candidate_inverted": row["candidate_inverted"],
                "translation_rms_m": row["translation_rms_m"],
                "rotation_rms_deg": row["rotation_rms_deg"],
                "score": row["score"],
            }
            for row in diagnostic
        ],
        "valid": bool(best["translation_rms_m"] <= max_trans and best["rotation_rms_deg"] <= max_rot),
    }
    if not record["valid"]:
        return {"ok": False, "reason": "handeye_residual_too_high", **record}
    return {"ok": True, **record}


def _transform_distance(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    ta = np.asarray(a, dtype=float).reshape(4, 4)
    tb = np.asarray(b, dtype=float).reshape(4, 4)
    trans_m = float(np.linalg.norm(ta[:3, 3] - tb[:3, 3]))
    rot_deg = math.degrees(float(np.linalg.norm(_rotation_vector(ta[:3, :3] @ tb[:3, :3].T))))
    return trans_m, rot_deg


def sample_pose_novelty(
    samples: list[dict[str, Any]],
    candidate: dict[str, Any],
    *,
    soft: bool = False,
) -> dict[str, Any]:
    cand = np.asarray(candidate.get("T_base_tool"), dtype=float)
    if cand.shape != (4, 4):
        return {"ok": False, "reason": "candidate_transform_invalid"}
    if not samples:
        return {
            "ok": True,
            "useful": True,
            "reason": "first_sample",
            "min_translation_delta_m": None,
            "min_rotation_delta_deg": None,
        }
    distances: list[tuple[float, float]] = []
    for sample in samples:
        old = np.asarray(sample.get("T_base_tool"), dtype=float)
        if old.shape == (4, 4):
            distances.append(_transform_distance(cand, old))
    if not distances:
        return {"ok": False, "reason": "no_valid_existing_samples"}
    min_trans = min(item[0] for item in distances)
    min_rot = min(item[1] for item in distances)
    # Default piu' alti: viste troppo vicine danno residual cm e pick impreciso.
    min_trans_req = float(os.environ.get("D1_GRASP6D_CALIB_MIN_NEW_TRANSLATION_M", "0.04"))
    min_rot_req = float(os.environ.get("D1_GRASP6D_CALIB_MIN_NEW_ROTATION_DEG", "12.0"))
    if soft:
        min_trans_req = min(min_trans_req, float(os.environ.get("D1_GRASP6D_CALIB_SOFT_NEW_TRANSLATION_M", "0.025")))
        min_rot_req = min(min_rot_req, float(os.environ.get("D1_GRASP6D_CALIB_SOFT_NEW_ROTATION_DEG", "8.0")))
    useful = bool(min_trans >= min_trans_req or min_rot >= min_rot_req)
    return {
        "ok": True,
        "useful": useful,
        "reason": "new_view_good" if useful else "pose_too_similar",
        "soft": soft,
        "min_translation_delta_m": min_trans,
        "min_rotation_delta_deg": min_rot,
        "required_translation_delta_m": min_trans_req,
        "required_rotation_delta_deg": min_rot_req,
    }


def handeye_quality_report(samples: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(samples)
    valid_samples = []
    for sample in samples:
        Tg = np.asarray(sample.get("T_base_tool"), dtype=float)
        Tc = np.asarray(sample.get("T_camera_target"), dtype=float)
        if (
            Tg.shape == (4, 4)
            and Tc.shape == (4, 4)
            and _safe_inv4(Tg) is not None
            and _safe_inv4(Tc) is not None
        ):
            valid_samples.append(sample)
    translations = []
    rotations = []
    for sample in valid_samples:
        T = np.asarray(sample["T_base_tool"], dtype=float)
        translations.append(T[:3, 3])
        rotations.append(T[:3, :3])
    trans_span = 0.0
    rot_span = 0.0
    if len(translations) >= 2:
        pts = np.stack(translations)
        trans_span = float(max(np.linalg.norm(a - b) for a in pts for b in pts))
        rot_span = float(
            max(
                math.degrees(float(np.linalg.norm(_rotation_vector(a @ b.T))))
                for a in rotations
                for b in rotations
            )
        )
    target_trans_span = float(os.environ.get("D1_GRASP6D_CALIB_TARGET_TRANSLATION_SPAN_M", "0.10"))
    # 25° e' raggiungibile dal D1 su AprilGrid senza orbitare troppo.
    target_rot_span = float(os.environ.get("D1_GRASP6D_CALIB_TARGET_ROTATION_SPAN_DEG", "25.0"))
    count_score = min(1.0, count / 8.0)
    trans_score = min(1.0, trans_span / max(target_trans_span, 1e-6))
    rot_score = min(1.0, rot_span / max(target_rot_span, 1e-6))
    diversity_score = 0.5 * trans_score + 0.5 * rot_score
    marker_rows = [s.get("marker") for s in valid_samples if isinstance(s.get("marker"), dict)]
    avg_tags = float(np.mean([float(m.get("visible_marker_count") or 0.0) for m in marker_rows])) if marker_rows else None
    avg_img_rms = (
        float(np.mean([float(m.get("reprojection_rms_px")) for m in marker_rows if m.get("reprojection_rms_px") is not None]))
        if marker_rows
        else None
    )
    min_n = calib_min_samples()
    solve = solve_handeye_calibration(valid_samples) if len(valid_samples) >= min_n else {
        "ok": False,
        "reason": "need_more_samples_for_residual",
        "sample_count": len(valid_samples),
        "min_samples": min_n,
    }
    max_trans_rms = float(os.environ.get("D1_GRASP6D_CALIB_MAX_RMS_M", "0.025"))
    max_rot_rms = float(os.environ.get("D1_GRASP6D_CALIB_MAX_RMS_DEG", "6.0"))
    residual_trend: list[dict[str, Any]] = []
    for n in range(min_n, len(valid_samples) + 1):
        partial = solve_handeye_calibration(valid_samples[:n])
        residual_trend.append(
            {
                "sample_count": n,
                "ok": bool(partial.get("ok")),
                "reason": partial.get("reason"),
                "translation_rms_m": partial.get("translation_rms_m"),
                "rotation_rms_deg": partial.get("rotation_rms_deg"),
            }
        )
    residual_score = 0.0
    if solve.get("translation_rms_m") is not None and solve.get("rotation_rms_deg") is not None:
        trans_ratio = float(solve["translation_rms_m"]) / max(max_trans_rms, 1e-6)
        rot_ratio = float(solve["rotation_rms_deg"]) / max(max_rot_rms, 1e-6)
        residual_score = max(0.0, min(1.0, 1.0 - 0.5 * (trans_ratio + rot_ratio - 2.0) / 8.0))
        if solve.get("ok"):
            residual_score = 1.0
    progress = int(round(100.0 * (0.35 * count_score + 0.30 * diversity_score + 0.35 * residual_score)))
    if not solve.get("ok"):
        progress = min(progress, 95)
    build_ready = bool(solve.get("ok") and diversity_score >= 0.70)
    residual_extreme = bool(
        solve.get("translation_rms_m") is not None
        and solve.get("rotation_rms_deg") is not None
        and (float(solve["translation_rms_m"]) > 0.12 or float(solve["rotation_rms_deg"]) > 20.0)
    )
    residual_high = bool(
        solve.get("translation_rms_m") is not None
        and solve.get("rotation_rms_deg") is not None
        and not solve.get("ok")
    )
    if count < min_n:
        next_action = "aggiungi_sample"
    elif diversity_score < 0.70:
        next_action = "aumenta_diversita_pose"
    elif residual_extreme:
        next_action = "sessione_incoerente_reset"
    elif residual_high:
        next_action = "prune_and_aggiungi_sample"
    elif not build_ready:
        next_action = "residuo_alto_non_calcolare"
    else:
        next_action = "calcola_handeye"
    sample_debug: list[dict[str, Any]] = []
    trans_errors_raw = solve.get("sample_translation_errors_m")
    rot_errors_raw = solve.get("sample_rotation_errors_deg")
    centers_raw = solve.get("base_target_centers_m")
    for index, sample in enumerate(valid_samples):
        marker = sample.get("marker") if isinstance(sample.get("marker"), dict) else {}
        row: dict[str, Any] = {
            "index": index + 1,
            "at": sample.get("at"),
            "servo_deg": sample.get("servo_deg"),
            "visible_marker_count": marker.get("visible_marker_count"),
            "reprojection_rms_px": marker.get("reprojection_rms_px"),
            "pose_method": marker.get("pose_method"),
        }
        if isinstance(trans_errors_raw, list) and index < len(trans_errors_raw):
            row["translation_error_m"] = trans_errors_raw[index]
        if isinstance(rot_errors_raw, list) and index < len(rot_errors_raw):
            row["rotation_error_deg"] = rot_errors_raw[index]
        if isinstance(centers_raw, list) and index < len(centers_raw):
            row["base_target_center_m"] = centers_raw[index]
        sample_debug.append(row)
    worst_sample = None
    if sample_debug:
        worst_sample = max(
            sample_debug,
            key=lambda row: float(row.get("translation_error_m") or 0.0) / 0.01
            + float(row.get("rotation_error_deg") or 0.0) / 3.0,
        )
    return {
        "ok": True,
        "sample_count": count,
        "valid_sample_count": len(valid_samples),
        "progress_percent": progress,
        "build_ready": build_ready,
        "next_action": next_action,
        "translation_span_m": trans_span,
        "rotation_span_deg": rot_span,
        "target_translation_span_m": target_trans_span,
        "target_rotation_span_deg": target_rot_span,
        "max_translation_rms_m": max_trans_rms,
        "max_rotation_rms_deg": max_rot_rms,
        "diversity_score": diversity_score,
        "avg_visible_marker_count": avg_tags,
        "avg_reprojection_rms_px": avg_img_rms,
        "sample_debug": sample_debug,
        "worst_sample": worst_sample,
        "residual_trend": residual_trend,
        "residual": solve,
    }


def residual_severity(report: dict[str, Any]) -> float | None:
    residual = report.get("residual") if isinstance(report, dict) else None
    if not isinstance(residual, dict):
        return None
    trans = residual.get("translation_rms_m")
    rot = residual.get("rotation_rms_deg")
    if trans is None or rot is None:
        return None
    max_trans = max(float(report.get("max_translation_rms_m") or 0.025), 1e-6)
    max_rot = max(float(report.get("max_rotation_rms_deg") or 6.0), 1e-6)
    return float(trans) / max_trans + float(rot) / max_rot


def detect_calibration_marker(color_bgr: np.ndarray, intrinsics: dict[str, Any]) -> dict[str, Any]:
    """Pose AprilGrid, ArUco singolo o scacchiera nel frame ottico camera."""
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
        def detect(dictionary: Any) -> tuple[list[Any], Any]:
            if hasattr(cv2.aruco, "ArucoDetector"):
                params = cv2.aruco.DetectorParameters()
                if hasattr(params, "cornerRefinementMethod") and hasattr(cv2.aruco, "CORNER_REFINE_SUBPIX"):
                    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
                found_corners, found_ids, _ = cv2.aruco.ArucoDetector(dictionary, params).detectMarkers(color_bgr)
            else:
                found_corners, found_ids, _ = cv2.aruco.detectMarkers(color_bgr, dictionary)
            return found_corners, found_ids

        # Target rilevato sul robot: AprilTag 36h11, griglia 6x4, ID
        # consecutivi 288..311. Tutti i corner visibili concorrono alla PnP,
        # rendendo la posa molto più stabile di un singolo marker.
        april_cols = int(os.environ.get("D1_GRASP6D_APRILGRID_COLS", "6"))
        april_rows = int(os.environ.get("D1_GRASP6D_APRILGRID_ROWS", "4"))
        april_first_id = int(os.environ.get("D1_GRASP6D_APRILGRID_FIRST_ID", "288"))
        april_tag_size = float(os.environ.get("D1_GRASP6D_APRILGRID_TAG_SIZE_M", "0.030"))
        april_gap = float(os.environ.get("D1_GRASP6D_APRILGRID_GAP_M", "0.015"))
        april_min_tags = max(2, int(os.environ.get("D1_GRASP6D_APRILGRID_MIN_TAGS", "4")))
        april_dict_id = getattr(cv2.aruco, "DICT_APRILTAG_36h11", None)
        if april_dict_id is not None:
            april_dictionary = cv2.aruco.getPredefinedDictionary(april_dict_id)
            april_corners, april_ids_raw = detect(april_dictionary)
            april_ids = [] if april_ids_raw is None else [int(x) for x in april_ids_raw.reshape(-1)]
            board_last_id = april_first_id + april_cols * april_rows
            visible = [
                (tag_id, np.asarray(tag_corners, dtype=np.float32).reshape(4, 2))
                for tag_id, tag_corners in zip(april_ids, april_corners)
                if april_first_id <= tag_id < board_last_id
            ]
            if len(visible) >= april_min_tags:
                pitch = april_tag_size + april_gap
                image_rows: list[list[float]] = []
                center_object_rows: list[list[float]] = []
                center_image_rows: list[list[float]] = []
                corner_object_variants: dict[str, list[list[float]]] = {
                    "xy_down": [],
                    "y_up": [],
                    "x_flip": [],
                    "xy_flip": [],
                }
                for tag_id, image_corners in visible:
                    offset = tag_id - april_first_id
                    row, col = divmod(offset, april_cols)
                    x0, y0 = col * pitch, row * pitch
                    center_object_rows.append([x0 + april_tag_size * 0.5, y0 + april_tag_size * 0.5, 0.0])
                    center_image_rows.append(np.mean(image_corners, axis=0).tolist())
                    x1, y1 = x0 + april_tag_size, y0 + april_tag_size
                    corner_object_variants["xy_down"].extend(
                        [[x0, y0, 0.0], [x1, y0, 0.0], [x1, y1, 0.0], [x0, y1, 0.0]]
                    )
                    corner_object_variants["y_up"].extend(
                        [[x0, y1, 0.0], [x1, y1, 0.0], [x1, y0, 0.0], [x0, y0, 0.0]]
                    )
                    corner_object_variants["x_flip"].extend(
                        [[x1, y0, 0.0], [x0, y0, 0.0], [x0, y1, 0.0], [x1, y1, 0.0]]
                    )
                    corner_object_variants["xy_flip"].extend(
                        [[x1, y1, 0.0], [x0, y1, 0.0], [x0, y0, 0.0], [x1, y0, 0.0]]
                    )
                    image_rows.extend(image_corners.tolist())
                image_points = np.asarray(image_rows, dtype=np.float32)
                center_object_points = np.asarray(center_object_rows, dtype=np.float32)
                center_image_points = np.asarray(center_image_rows, dtype=np.float32)
                max_rms = float(os.environ.get("D1_GRASP6D_APRILGRID_MAX_REPROJECTION_PX", "2.0"))

                def solve_and_score(
                    obj: np.ndarray,
                    img: np.ndarray,
                    *,
                    flags: int = cv2.SOLVEPNP_ITERATIVE,
                ) -> tuple[bool, Any, Any, float]:
                    ok, rvec, tvec = cv2.solvePnP(obj, img, K, dist, flags=flags)
                    if ok and hasattr(cv2, "solvePnPRefineLM"):
                        rvec, tvec = cv2.solvePnPRefineLM(obj, img, K, dist, rvec, tvec)
                    if not ok:
                        return False, rvec, tvec, float("inf")
                    projected, _ = cv2.projectPoints(obj, rvec, tvec, K, dist)
                    rms = float(np.sqrt(np.mean(np.sum((projected.reshape(-1, 2) - img) ** 2, axis=1))))
                    return True, rvec, tvec, rms

                corner_results = []
                for variant_name, object_rows in corner_object_variants.items():
                    object_points = np.asarray(object_rows, dtype=np.float32)
                    ok, rvec, tvec, rms = solve_and_score(object_points, image_points)
                    corner_results.append((variant_name, ok, rvec, tvec, rms, object_points))
                corner_variant, corner_ok, corner_rvec, corner_tvec, corner_rms, best_object_points = min(
                    corner_results,
                    key=lambda item: item[4],
                )
                center_ok, center_rvec, center_tvec, center_rms = solve_and_score(
                    center_object_points,
                    center_image_points,
                )
                chosen: tuple[str, Any, Any, float, np.ndarray, str | None] | None = None
                if corner_ok and corner_rms <= max_rms:
                    chosen = ("tag_corners", corner_rvec, corner_tvec, corner_rms, image_points, corner_variant)
                elif center_ok and center_rms <= max_rms:
                    chosen = ("tag_centers", center_rvec, center_tvec, center_rms, center_image_points, None)
                if chosen is not None:
                    pose_method, rvec, tvec, reprojection_rms, returned_points, object_variant = chosen
                    R, _ = cv2.Rodrigues(rvec)
                    out = {
                        "ok": True,
                        "target_type": "aprilgrid_36h11",
                        "dictionary": "DICT_APRILTAG_36h11",
                        "grid_cols_rows": [april_cols, april_rows],
                        "first_marker_id": april_first_id,
                        "marker_ids": sorted(tag_id for tag_id, _ in visible),
                        "visible_marker_count": len(visible),
                        "marker_size_m": april_tag_size,
                        "marker_gap_m": april_gap,
                        "pose_method": pose_method,
                        "object_point_variant": object_variant,
                        "reprojection_rms_px": round(reprojection_rms, 4),
                        "T_camera_target": _transform(R, np.asarray(tvec).reshape(3)).tolist(),
                        "corners_px": returned_points.tolist(),
                    }
                    if corner_ok:
                        out["corner_reprojection_rms_px"] = round(corner_rms, 4)
                        out["corner_object_variant"] = corner_variant
                        out["corner_variant_rms_px"] = {
                            str(name): (None if not ok else round(float(rms), 4))
                            for name, ok, _, _, rms, _ in corner_results
                        }
                    if center_ok:
                        out["center_reprojection_rms_px"] = round(center_rms, 4)
                    return out
                if corner_ok or center_ok:
                    return {
                        "ok": False,
                        "reason": "aprilgrid_reprojection_too_high",
                        "dictionary": "DICT_APRILTAG_36h11",
                        "visible_marker_count": len(visible),
                        "marker_ids": sorted(tag_id for tag_id, _ in visible),
                        "corner_reprojection_rms_px": None if not corner_ok else round(corner_rms, 4),
                        "center_reprojection_rms_px": None if not center_ok else round(center_rms, 4),
                        "max_reprojection_rms_px": max_rms,
                    }
            if april_ids:
                return {
                    "ok": False,
                    "reason": "aprilgrid_not_enough_expected_tags",
                    "dictionary": "DICT_APRILTAG_36h11",
                    "detected_apriltag_ids": sorted(april_ids),
                    "expected_marker_ids": [april_first_id, board_last_id - 1],
                    "visible_marker_count": len(visible),
                    "minimum_visible_marker_count": april_min_tags,
                }

        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        corners, ids = detect(dictionary)
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


def calibration_history(*, limit: int = 40) -> list[dict[str, Any]]:
    try:
        data = json.loads(CALIBRATION_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data if isinstance(data, list) else []
    return [row for row in rows[-max(1, int(limit)) :] if isinstance(row, dict)]


def record_calibration_event(event: str, **payload: Any) -> dict[str, Any]:
    rows = calibration_history(limit=200)
    row = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "event": str(event),
        **payload,
    }
    rows.append(row)
    rows = rows[-200:]
    CALIBRATION_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CALIBRATION_HISTORY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(CALIBRATION_HISTORY_PATH)
    return row


def save_handeye_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    HAND_EYE_SAMPLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = HAND_EYE_SAMPLES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(list(samples), indent=2), encoding="utf-8")
    tmp.replace(HAND_EYE_SAMPLES_PATH)
    return {"ok": True, "sample_count": len(samples), "path": str(HAND_EYE_SAMPLES_PATH)}


def append_handeye_sample(
    T_base_tool: np.ndarray,
    T_camera_target: np.ndarray,
    *,
    marker: dict[str, Any] | None = None,
    servo_deg: list[float] | None = None,
) -> dict[str, Any]:
    samples = list_handeye_samples()
    marker_summary = {
        key: marker.get(key)
        for key in (
            "target_type",
            "visible_marker_count",
            "pose_method",
            "reprojection_rms_px",
            "corner_reprojection_rms_px",
            "center_reprojection_rms_px",
            "marker_ids",
        )
        if isinstance(marker, dict) and key in marker
    }
    row = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "T_base_tool": np.asarray(T_base_tool, dtype=float).reshape(4, 4).tolist(),
        "T_camera_target": np.asarray(T_camera_target, dtype=float).reshape(4, 4).tolist(),
        "marker": marker_summary,
    }
    if servo_deg is not None:
        row["servo_deg"] = [float(x) for x in servo_deg[:7]]
    samples.append(row)
    save_handeye_samples(samples)
    return {"ok": True, "sample_count": len(samples), "path": str(HAND_EYE_SAMPLES_PATH)}


def prune_handeye_outliers(
    *,
    min_keep: int | None = None,
    max_drop: int = 5,
    force_drop: int = 0,
) -> dict[str, Any]:
    """Toglie i sample peggiori finche' il solve e' valido, fino a min_keep.

    Con force_drop>0 elimina comunque i peggiori (sblocca sessioni bloccate a N=min_keep).
    """
    samples = list_handeye_samples()
    keep = int(min_keep if min_keep is not None else calib_min_samples())
    keep = max(calib_min_samples(), keep)
    if len(samples) < keep and force_drop <= 0:
        return {
            "ok": False,
            "reason": "not_enough_samples_to_prune",
            "sample_count": len(samples),
            "min_keep": keep,
        }
    max_t = float(os.environ.get("D1_GRASP6D_CALIB_MAX_RMS_M", "0.025"))
    max_r = float(os.environ.get("D1_GRASP6D_CALIB_MAX_RMS_DEG", "6.0"))
    working = list(samples)
    dropped: list[dict[str, Any]] = []
    sol = solve_handeye_calibration(working) if len(working) >= calib_min_samples() else {
        "ok": False,
        "reason": "need_more_samples_for_residual",
    }

    def _drop_worst(current_sol: dict[str, Any]) -> bool:
        nonlocal working, sol
        t_err = current_sol.get("sample_translation_errors_m") or []
        r_err = current_sol.get("sample_rotation_errors_deg") or []
        if len(t_err) != len(working) or len(r_err) != len(working) or not working:
            return False
        scores = [
            float(t) / max(max_t, 1e-6) + float(r) / max(max_r, 1e-6) for t, r in zip(t_err, r_err)
        ]
        worst = int(np.argmax(np.asarray(scores, dtype=float)))
        dropped.append(
            {
                "index": worst,
                "score": scores[worst],
                "translation_error_m": float(t_err[worst]),
                "rotation_error_deg": float(r_err[worst]),
                "servo_deg": working[worst].get("servo_deg"),
                "visible_marker_count": (working[worst].get("marker") or {}).get("visible_marker_count"),
            }
        )
        working = [row for index, row in enumerate(working) if index != worst]
        sol = (
            solve_handeye_calibration(working)
            if len(working) >= calib_min_samples()
            else {"ok": False, "reason": "need_more_samples_for_residual", "sample_count": len(working)}
        )
        return True

    while (not sol.get("ok")) and len(working) > keep and len(dropped) < max_drop:
        if not _drop_worst(sol):
            break
    forced = 0
    while forced < max(0, int(force_drop)) and len(working) > keep and len(dropped) < max_drop + max(0, int(force_drop)):
        if not sol.get("sample_translation_errors_m"):
            break
        if not _drop_worst(sol):
            break
        forced += 1
    # Se ancora invalido: prova leave-k-out (k=1..3) senza esplodere combinatorio.
    subset_search = None
    if (not sol.get("ok")) and keep <= len(working) <= 12:
        import itertools

        best_subset: list[dict[str, Any]] | None = None
        best_sol: dict[str, Any] | None = None
        best_score = float("inf")
        max_remove = min(3, len(working) - keep)
        for remove_n in range(1, max_remove + 1):
            for drop_idxs in itertools.combinations(range(len(working)), remove_n):
                drop_set = set(drop_idxs)
                subset = [row for index, row in enumerate(working) if index not in drop_set]
                cand = solve_handeye_calibration(subset)
                if cand.get("translation_rms_m") is None or cand.get("rotation_rms_deg") is None:
                    continue
                score = float(cand["translation_rms_m"]) / max(max_t, 1e-6) + float(
                    cand["rotation_rms_deg"]
                ) / max(max_r, 1e-6)
                if score < best_score:
                    best_score = score
                    best_subset = subset
                    best_sol = cand
            if best_sol and best_sol.get("ok"):
                break
        if best_subset is not None and best_sol is not None and (
            best_sol.get("ok")
            or best_score
            < (
                float(sol.get("translation_rms_m") or 1.0) / max(max_t, 1e-6)
                + float(sol.get("rotation_rms_deg") or 1e3) / max(max_r, 1e-6)
            )
        ):
            kept_ids = {id(row) for row in best_subset}
            for index, row in enumerate(working):
                if id(row) not in kept_ids:
                    dropped.append({"index": index, "reason": "subset_search_drop"})
            working = best_subset
            sol = best_sol
            subset_search = {"size": len(working), "score": best_score, "ok": bool(sol.get("ok"))}
    changed = len(working) != len(samples)
    if changed:
        save_handeye_samples(working)
    built = build_handeye_calibration(working) if sol.get("ok") else None
    return {
        "ok": True,
        "changed": changed,
        "before": len(samples),
        "after": len(working),
        "dropped": dropped,
        "forced_drops": forced,
        "min_keep": keep,
        "subset_search": subset_search,
        "solve": sol,
        "build": built,
    }


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

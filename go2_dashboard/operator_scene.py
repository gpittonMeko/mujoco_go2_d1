"""
Payload ``/api/arm/scene_3d`` ridotto per la dashboard operator.

Niente ``api_box_plan`` / AprilTag planner: solo FK servo (o fold), geometria da
``data/vis_geometry_tuning.json`` + default, marker coerenti con il viewer JS.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from typing import Any

import numpy as np

from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag
from go2_dashboard.operator_stack import go2_local
from go2_dashboard.paths import PROJECT_ROOT

_VIS_DEFAULTS: dict[str, float] = {
    "arm_vs_tag5_x": -0.19,
    "arm_vs_tag5_y": 0.0,
    "arm_vs_tag5_z": -0.08,
    "front_vs_tag5_x": 0.185,
    "front_vs_tag5_y": 0.0,
    "front_vs_tag5_z": -0.07,
    "wrist_local_dx": 0.0,
    "wrist_local_dy": 0.0,
    "wrist_local_dz": 0.0,
    "target_ema_alpha": float(os.environ.get("GO2_SCENE3D_TARGET_EMA_ALPHA", "0.28")),
    "viz_go2_tx_m": 0.0,
    "viz_go2_ty_m": 0.0,
    "viz_go2_tz_m": 0.0,
    "viz_joint_markers_dx_m": -0.15,
    "viz_joint_markers_dy_m": 0.0,
    "viz_joint_markers_dz_m": 0.0,
    "viz_arm_mount_dx_m": -0.20,
    "viz_arm_mount_dy_m": 0.0,
    "viz_arm_mount_dz_m": 0.0,
    "viz_front_cam_dx_m": 0.225,
    "viz_front_cam_dy_m": 0.0,
    "viz_front_cam_dz_m": -0.05,
    "frustum_depth_rx_deg": 20.0,
    "frustum_depth_ry_deg": 0.0,
    "frustum_depth_rz_deg": 0.0,
    "frustum_wrist_rx_deg": 0.0,
    "frustum_wrist_ry_deg": 0.0,
    "frustum_wrist_rz_deg": 0.0,
    "frustum_depth_far_m": 0.62,
    "frustum_wrist_far_m": 0.58,
}

_TUNING_PATH = PROJECT_ROOT / "data" / "vis_geometry_tuning.json"


def _load_vis_geometry_effective() -> dict[str, float]:
    out = dict(_VIS_DEFAULTS)
    if not _TUNING_PATH.is_file():
        return out
    try:
        raw = json.loads(_TUNING_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            if k in _VIS_DEFAULTS:
                out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def build_scene_3d_payload(*, geometry_fast: bool) -> dict[str, Any]:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from arm_kinematics_d1_template import (
        ARM_FOLD_POSE,
        DEPTH_CAMERA_ARM_BASE_M,
        depth_camera_optical_axis_unit_arm_base,
        fk_chain_positions,
        fk_d1_joint_locals_m,
        fk_tool_tip,
        fk_wrist_camera_center_m,
        fk_wrist_camera_view_axis_unit_m,
        nominal_object_along_depth_optical_arm_m,
    )

    vg = _load_vis_geometry_effective()
    wrist_off: np.ndarray | None = None
    wo = (vg["wrist_local_dx"], vg["wrist_local_dy"], vg["wrist_local_dz"])
    if any(abs(wo[i]) > 1e-12 for i in range(3)):
        wrist_off = np.array(wo, dtype=float)

    q_vis: list[float] = [float(x) for x in ARM_FOLD_POSE]
    if go2_local():
        angles, servo_diag = read_servo_deg_with_diag(PROJECT_ROOT)
    else:
        angles, servo_diag = None, {"reason": "GO2_LOCAL=0_skip_servo_subprocess", "skipped": True}
    servo_ok = angles is not None and len(angles or []) >= 6
    if servo_ok:
        q_vis = [math.radians(float(angles[i])) for i in range(6)]

    _mount_nom = np.array([0.15, 0.0, 0.06], dtype=float)
    _mount_bl_list = [round(float(x), 5) for x in _mount_nom]
    _depth_vis_arm = np.asarray(DEPTH_CAMERA_ARM_BASE_M, dtype=float)
    _d_ax = depth_camera_optical_axis_unit_arm_base()

    def _ab_to_bl(p: list[float] | Any) -> list[float]:
        a = [float(np.asarray(p, dtype=float)[i]) for i in range(3)]
        return [round(a[i] + _mount_bl_list[i], 5) for i in range(3)]

    wc = fk_wrist_camera_center_m(q_vis, wrist_off)
    wv = fk_wrist_camera_view_axis_unit_m(q_vis, wrist_off)
    wc_mjcf = fk_wrist_camera_center_m(q_vis, None)
    wv_mjcf = fk_wrist_camera_view_axis_unit_m(q_vis, None)
    cam_sites: dict[str, Any] = {
        "logical_6_go2_front": {
            "label": "MJCF depth_camera (base_link)",
            "pos_arm_base_m": [round(float(_depth_vis_arm[i]), 5) for i in range(3)],
            "view_axis_unit_m": [round(float(_d_ax[i]), 5) for i in range(3)],
        },
        "logical_0_wrist": {
            "label": "wrist_camera (FK + offset)",
            "pos_arm_base_m": [round(float(wc[i]), 5) for i in range(3)],
            "view_axis_unit_m": [round(float(wv[i]), 5) for i in range(3)],
            "mjcf_pos_arm_base_m": [round(float(wc_mjcf[i]), 5) for i in range(3)],
            "mjcf_view_axis_unit_m": [round(float(wv_mjcf[i]), 5) for i in range(3)],
        },
    }

    front_cam_display_bl = _ab_to_bl(_depth_vis_arm.tolist())
    wrist_cam_display_bl = _ab_to_bl(wc.tolist())

    tip_v = fk_tool_tip(q_vis)
    _ch_bl = fk_chain_positions(q_vis)
    _jmark_bias = (
        float(vg["viz_joint_markers_dx_m"]),
        float(vg["viz_joint_markers_dy_m"]),
        float(vg["viz_joint_markers_dz_m"]),
    )
    scene_graph: dict[str, Any] = {
        "frame": "base_link",
        "arm_mount_xyz_m": [round(float(x), 5) for x in _mount_nom],
        "arm_base_to_base_link_offset_m": [round(float(x), 5) for x in _mount_nom],
        "d1_joint_locals_m": fk_d1_joint_locals_m(q_vis),
        "d1_joint_centers_base_link_m": [
            _ab_to_bl([round(float(_ch_bl[i][j]) + _jmark_bias[j], 5) for j in range(3)])
            for i in range(1, 7)
        ],
        "tool_tip_xyz_m": [round(float(tip_v[i]), 5) for i in range(3)],
        "pose_is_feedback": bool(servo_ok),
        "go2_body_offset_base_link_m": [
            round(float(vg["viz_go2_tx_m"]), 5),
            round(float(vg["viz_go2_ty_m"]), 5),
            round(float(vg["viz_go2_tz_m"]), 5),
        ],
    }

    _nom_dep = float(os.environ.get("GO2_OBJECT_NOMINAL_DEPTH_ALONG_OPTICAL_M", "0.20"))
    _nom_vec = np.asarray(nominal_object_along_depth_optical_arm_m(_nom_dep), dtype=float)
    _nom_obj_ab = _nom_vec

    lm: dict[str, Any] = {
        "depth_camera_mjcf_m": _ab_to_bl(_depth_vis_arm.tolist()),
        "wrist_camera_mjcf_m": _ab_to_bl(wc.tolist()),
        "front_camera_display_base_link_m": [round(float(x), 5) for x in front_cam_display_bl],
        "wrist_camera_display_base_link_m": [round(float(x), 5) for x in wrist_cam_display_bl],
        "xt16_tag_m": None,
        "object_nominal_20cm_base_link_m": _ab_to_bl(_nom_obj_ab.tolist()),
        "object_target_base_link_m": None,
        "object_target_display_base_link_m": None,
        "object_grasp_target_display_base_link_m": None,
        "tool_tip_base_link_m": _ab_to_bl([round(float(tip_v[i]), 5) for i in range(3)]),
    }

    payload: dict[str, Any] = {
        "ok": True,
        "frame": "arm_base",
        "operator_scene": True,
        "geometry_fast_preview": bool(geometry_fast),
        "geometry_full_requested": not geometry_fast,
        "operator_scene_note_it": (
            "Dashboard operator: niente piano AprilTag/``api_box_plan`` qui. "
            "Solo FK + geometria file; ``full=1`` non avvia il planner del monolite."
        ),
        "axes_hint": {
            "x": "avanti (davanti al cane)",
            "y": "sinistra",
            "z": "su",
            "unit": "m",
            "three_js_note": "Viewer: assi come nel monolite.",
        },
        "vis_geometry_effective": {k: round(float(v), 6) for k, v in vg.items()},
        "mujoco_camera_sites_arm_m": cam_sites,
        "apriltag_tag_estimates_base_m": [],
        "vis_geometry_markers_arm_m": {
            "tag5_estimated_m": None,
            "arm_mount_m": None,
            "front_camera_from_tag5_m": None,
            "mjcf_depth_camera_m": [round(float(_depth_vis_arm[i]), 5) for i in range(3)],
            "mjcf_wrist_camera_m": [round(float(fk_wrist_camera_center_m(q_vis, None)[i]), 5) for i in range(3)],
            "wrist_camera_display_m": [round(float(wc[i]), 5) for i in range(3)],
            "object_nominal_along_mjcf_optical_arm_m": [
                round(float(_nom_obj_ab[i]), 5) for i in range(3)
            ],
            "note": "Operator slim — tag5/target da visione non popolati.",
        },
        "vis_geometry_chain_mm": None,
        "calibration_visual_alignment": {
            "planner_viewer_tag_positions_aligned": False,
            "tag5_visible_in_plan": False,
            "align_hint_it": "Usa il monolite solo se ti serve il piano tag completo.",
        },
        "nominal_object_depth_along_optical_m": round(_nom_dep, 5),
        "go2_silhouette_anchor_arm_m": None,
        "vision_snapshot": {
            "planner_ok": False,
            "logical_camera_used": None,
            "tag_ids_in_selected_frame": [],
            "grip_point_ok": False,
            "grip_source": None,
            "target_ok": False,
            "preview_ik_ok": False,
            "geometry_fast_preview": bool(geometry_fast),
            "hint": "Solo viewer operator — nessun /api/box/plan.",
        },
        "tags_for_viewer": [],
        "viewer_cameras_base_link_m": {
            "depth_front_arm_base_m": [round(float(_depth_vis_arm[i]), 5) for i in range(3)],
            "depth_front_base_link_m": _ab_to_bl(_depth_vis_arm.tolist()),
            "front_display_base_link_m": [round(float(x), 5) for x in front_cam_display_bl],
            "wrist_arm_base_m": [round(float(wc[i]), 5) for i in range(3)],
            "wrist_base_link_m": wrist_cam_display_bl,
            "wrist_display_base_link_m": [round(float(x), 5) for x in wrist_cam_display_bl],
        },
        "viewer_detected_object_primitive": {
            "kind": "box",
            "center_base_link_m": None,
            "size_m": [0.12, 0.08, 0.08],
            "note_it": "Nessun oggetto da planner su questa dashboard.",
        },
        "viewer_landmarks_base_link_m": lm,
        "scene_graph": scene_graph,
        "ik_trajectory": {"targets_xyz_m": [], "fk_tool_xyz_m": [], "ghost_chains_m": [], "stages": []},
        "viewer_3d_warnings": [],
        "scene_mesh": {"manifest": [], "api_pattern": "/api/arm/scene_meshes/<go2|d1>/<filename> (non montato qui)"},
    }

    if servo_ok:
        payload["servo_feedback_ok"] = True
        payload["joints_deg"] = [round(float(angles[i]), 3) for i in range(min(7, len(angles)))]
        payload["chain_xyz_m"] = fk_chain_positions(q_vis)
        payload["tool_tip_xyz_m"] = [round(float(tip_v[i]), 5) for i in range(3)]
        payload["servo_feedback_diag"] = {"reason": "OK", **{k: v for k, v in servo_diag.items() if k in (
            "backend", "backends_tried", "duration_s", "dds_domain", "listen_s", "helper_path",
        )}}
    else:
        payload["servo_feedback_ok"] = False
        payload["servo_feedback_diag"] = servo_diag

    return payload


def build_grasp_pipeline_stub() -> dict[str, Any]:
    """Messaggio chiaro: la pipeline end-to-end del monolite non è duplicata qui."""
    return {
        "ok": True,
        "operator_slim": True,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "fusion_ready_for_execute": False,
        "narrative_it": [
            "① Dashboard operator autonoma: niente import del processo monolite.",
            "② Per ``fusion_ready_for_execute`` / piano tag/IK completo usa la dashboard classica "
            "(``serve_dashboard_modular`` / porta 5050) oppure estendi ``go2_dashboard.operator_scene``.",
            "③ Controlla camere con GET /api/cameras/status e viewer 3D con GET /api/arm/scene_3d.",
        ],
        "environment": {
            "GO2_LOCAL": os.environ.get("GO2_LOCAL", "0"),
            "GO2_ENABLE_REAL_ARM": os.environ.get("GO2_ENABLE_REAL_ARM", "0"),
            "GO2_GRASP_EXECUTE_ARM": os.environ.get("GO2_GRASP_EXECUTE_ARM", "0"),
        },
        "selected_camera": None,
        "selected_grasp_assessment": None,
        "sequence_start_ready": False,
        "sequence_start_block_reason": "operator_dashboard_no_monolith_pipeline",
    }

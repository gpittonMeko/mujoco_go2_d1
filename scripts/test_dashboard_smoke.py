#!/usr/bin/env python3
"""
Smoke test automatico (senza robot): import Flask, route HTTP, planner AprilTag su frame vuoto.

Esegui dalla root del repo:
  python scripts/test_dashboard_smoke.py

Exit 0 se OK, exit 1 se fallisce (CI / pre-deploy locale).

Da eseguire sul PC di sviluppo (OpenCV ArUco su alcuni Jetson può andare in segfault con frame sintetici — non usare come gate sulla NX).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "scripts"))

    # Evita che un vis_geometry_tuning.json residuo alteri i default al primo import.
    _vis_t = root / "data" / "vis_geometry_tuning.json"
    if _vis_t.is_file():
        try:
            _vis_t.unlink()
        except OSError:
            pass
    _vis_p = root / "data" / "vis_geometry_presets.json"
    if _vis_p.is_file():
        try:
            _vis_p.unlink()
        except OSError:
            pass

    os.environ.setdefault("GO2_LOCAL", "0")
    os.environ.setdefault("GO2_ENABLE_REAL_ARM", "0")
    os.environ.setdefault("GO2_ENABLE_BASE_MOTION", "0")
    os.environ.setdefault("GO2_GRASP_EXECUTE_ARM", "1")

    import diagnostics_dashboard as dd

    client = dd.APP.test_client()

    # --- HTTP ---
    r = client.get("/api/health")
    assert r.status_code == 200, (r.status_code, r.data)
    assert r.headers.get("Access-Control-Allow-Origin") == "*"
    j = r.get_json()
    assert j.get("ok") is True and j.get("service") == "go2_dashboard", j
    assert j.get("pid") is not None and j.get("process_started_at") and j.get("dashboard_py_mtime")

    r = client.get("/api/status")
    assert r.status_code == 200, r.data
    st = r.get_json()
    assert isinstance(st.get("tests"), dict) and "summary" in st

    import copy

    with dd.STATUS_LOCK:
        _prev_status = copy.deepcopy(dd.STATUS)
    try:
        dd.set_status(
            {
                "updated_at": dd.now_iso(),
                "running": False,
                "summary": "probe-nan",
                "tests": {"probe_nan": {"v": float("nan"), "ok": True}},
            }
        )
        r_nan = client.get("/api/status")
        assert r_nan.status_code == 200, r_nan.data
        jn = r_nan.get_json()
        assert jn["tests"]["probe_nan"]["v"] is None
    finally:
        dd.set_status(_prev_status)

    r = client.get("/")
    assert r.status_code == 200, r.status_code
    body = r.data.decode("utf-8", errors="replace")
    assert "Go2 Diagnostics Dashboard" in body
    assert "alwaysBasePosePill" in body and "basePose" in body
    assert "presaSequenceStatus" in body
    assert "tagSummary0" in body
    assert "graspPhaseBig" in body
    assert "gripStateBig" in body
    assert "detectorStateBig" in body
    assert "attemptGrasp()" in body
    assert "refreshDetectionNow()" in body
    assert 'id="alwaysCamStrip"' in body or "alwaysCamStrip" in body
    assert "aprilTagLog" in body
    assert "armMotionDiagPre" in body
    assert "tune_tag_wait_s" in body
    assert "graspPipelinePre" in body
    assert "saveTrueZeroPose" in body
    assert "gotoStartFromTrueZero" in body
    assert "gotoSavedStartPose" in body
    assert 'id="jointSlide0"' in body
    assert "jointSliderLiveInput" in body
    assert 'id="jointLiveEnabled"' in body
    assert 'id="alwaysBox0"' in body and "/tags.mjpg" in body

    r = client.get("/api/arm/diagnose_motion")
    assert r.status_code == 200
    dm = r.get_json()
    assert dm.get("ok") is True
    assert isinstance(dm.get("hints"), list)

    r = client.get("/api/arm/grasp_pipeline")
    assert r.status_code == 200
    gp = r.get_json()
    assert gp.get("ok") is True
    assert isinstance(gp.get("narrative_it"), list)
    assert "candidates" in gp
    assert "true_zero_json" in gp

    r = client.get("/api/arm/ui_tuning")
    assert r.status_code == 200
    ut = r.get_json()
    assert ut.get("ok") is True
    assert "effective" in ut and "tag_wait_s" in ut["effective"]
    assert r.status_code == 200
    assert r.get_json().get("ok") is True

    ro = client.open("/api/arm/vis_geometry", method="OPTIONS")
    assert ro.status_code in (200, 204)
    assert ro.headers.get("Access-Control-Allow-Origin") == "*"

    r = client.get("/api/arm/vis_geometry")
    assert r.status_code == 200
    vg0 = r.get_json()
    assert vg0.get("ok") is True
    eff0 = vg0.get("effective") or {}
    assert "arm_vs_tag5_x" in eff0 and "front_vs_tag5_x" in eff0
    assert "wrist_local_dx" in eff0 and "target_ema_alpha" in eff0
    assert "viz_go2_tx_m" in eff0 and abs(float(eff0["viz_go2_tx_m"])) < 1e-9
    assert abs(float(eff0["arm_vs_tag5_x"]) + 0.20) < 1e-5
    # Preset «2» all'avvio modulo (builtin se presets.json assente): MJCF polso/camera front nominale in kinematics.
    assert abs(float(eff0["wrist_local_dx"])) < 1e-8, eff0.get("wrist_local_dx")
    assert abs(float(eff0["wrist_local_dy"])) < 1e-8
    assert abs(float(eff0["wrist_local_dz"])) < 1e-8, eff0.get("wrist_local_dz")

    r = client.get("/api/arm/scene_3d?fast=1")
    assert r.status_code == 200
    s3b = r.get_json()
    assert s3b.get("ok") is True
    vgb = (s3b.get("vis_geometry_effective") or {})
    assert abs(float(vgb["wrist_local_dx"])) < 1e-8
    assert abs(float(vgb["wrist_local_dz"])) < 1e-8
    cva = s3b.get("calibration_visual_alignment")
    assert isinstance(cva, dict)
    assert cva.get("planner_viewer_tag_positions_aligned") is True

    r = client.get("/api/arm/calibration_flow")
    assert r.status_code == 200
    cf = r.get_json()
    assert cf.get("ok") is True
    assert isinstance(cf.get("steps_it"), list) and len(cf["steps_it"]) >= 3

    r = client.post(
        "/api/arm/vis_geometry",
        json={"arm_vs_tag5_x": -0.175, "target_ema_alpha": 0.31},
    )
    assert r.status_code == 200
    vg1 = r.get_json()
    assert vg1.get("ok") is True
    assert abs(float(vg1["effective"]["arm_vs_tag5_x"]) + 0.175) < 1e-5
    assert abs(float(vg1["effective"]["target_ema_alpha"]) - 0.31) < 1e-5

    r = client.post("/api/arm/vis_geometry", json={"reset": True})
    assert r.status_code == 200
    vg2 = r.get_json()
    assert vg2.get("ok") is True
    assert abs(float(vg2["effective"]["arm_vs_tag5_x"]) + 0.20) < 1e-5

    r = client.get("/api/arm/vis_geometry/presets")
    assert r.status_code == 200
    prl = r.get_json()
    assert prl.get("ok") is True
    assert isinstance(prl.get("presets"), list)

    r = client.post(
        "/api/arm/vis_geometry/presets/save",
        json={"name": "__smoke_preset__", "overwrite": True},
    )
    assert r.status_code == 200
    ps = r.get_json()
    assert ps.get("ok") is True
    assert int(ps.get("n_keys") or 0) == len(dd._ALLOWED_VIS_GEOMETRY)

    r = client.post(
        "/api/arm/vis_geometry/presets/load",
        json={"name": "__smoke_preset__", "persist": False},
    )
    assert r.status_code == 200
    assert r.get_json().get("ok") is True

    r = client.post("/api/arm/vis_geometry/presets/remove", json={"name": "__smoke_preset__"})
    assert r.status_code == 200
    rm = r.get_json()
    assert rm.get("removed") == "__smoke_preset__"

    r = client.post(
        "/api/arm/vis_geometry/presets/load",
        json={"name": "2", "persist": False},
    )
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:500])
    p2 = r.get_json()
    assert p2.get("name") == "2"
    assert p2.get("ok") is True
    effp2 = p2.get("effective") or {}
    assert abs(float(effp2["wrist_local_dx"])) < 1e-8
    assert abs(float(effp2["wrist_local_dz"])) < 1e-8

    # Round-trip preset (save → muta tuning → load ripristina) + 409 se nome già usato
    r = client.post(
        "/api/arm/vis_geometry",
        json={"viz_go2_tx_m": 0.042, "persist": False},
    )
    assert r.status_code == 200 and r.get_json().get("ok") is True
    r = client.post(
        "/api/arm/vis_geometry/presets/save",
        json={"name": "__rt_preset__", "overwrite": True},
    )
    assert r.status_code == 200
    rt = r.get_json()
    assert rt.get("ok") is True
    assert int(rt.get("n_keys") or 0) == len(dd._ALLOWED_VIS_GEOMETRY)

    r = client.post(
        "/api/arm/vis_geometry",
        json={"viz_go2_tx_m": -0.077, "persist": False},
    )
    assert r.status_code == 200
    assert abs(float(r.get_json()["effective"]["viz_go2_tx_m"]) + 0.077) < 1e-5

    r = client.post(
        "/api/arm/vis_geometry/presets/load",
        json={"name": "__rt_preset__", "persist": False},
    )
    assert r.status_code == 200
    ld = r.get_json()
    assert ld.get("ok") is True
    assert abs(float(ld["effective"]["viz_go2_tx_m"]) - 0.042) < 1e-5

    r = client.post(
        "/api/arm/vis_geometry/presets/save",
        json={"name": "__rt_preset__", "overwrite": False},
    )
    assert r.status_code == 409

    r = client.post("/api/arm/vis_geometry/presets/remove", json={"name": "__rt_preset__"})
    assert r.status_code == 200
    assert r.get_json().get("removed") == "__rt_preset__"

    r = client.post("/api/arm/vis_geometry", json={"reset": True})
    assert r.status_code == 200

    r = client.get("/api/arm/scene_3d")
    assert r.status_code == 200
    s3 = r.get_json()
    assert s3.get("ok") is True
    assert s3.get("frame") == "arm_base"
    assert isinstance(s3.get("servo_feedback_diag"), dict)
    assert "reason" in s3["servo_feedback_diag"]
    assert isinstance(s3.get("vis_geometry_effective"), dict)
    assert "arm_vs_tag5_x" in s3["vis_geometry_effective"]
    sg = s3.get("scene_graph")
    assert isinstance(sg, dict) and sg.get("frame") == "base_link"
    assert isinstance(sg.get("d1_joint_locals_m"), list) and len(sg["d1_joint_locals_m"]) == 6
    assert isinstance(sg.get("go2_body_offset_base_link_m"), list) and len(sg["go2_body_offset_base_link_m"]) == 3
    jc = sg.get("d1_joint_centers_base_link_m")
    assert isinstance(jc, list) and len(jc) == 6
    assert isinstance(s3.get("tags_for_viewer"), list)
    assert isinstance(s3.get("viewer_cameras_base_link_m"), dict)
    fru = s3.get("scene_camera_frusta_base_link")
    assert isinstance(fru, dict) and "depth_mjcf" in fru and "wrist" in fru
    assert fru["depth_mjcf"].get("fovy_deg") == 62.0
    assert fru["wrist"].get("fovy_deg") == 78.0
    assert len(fru["depth_mjcf"].get("axis_unit_m") or []) == 3
    assert fru["depth_mjcf"].get("near_m") == 0.02
    assert abs(float(fru["depth_mjcf"].get("far_m") or 0) - 0.62) < 1e-6
    assert abs(float(fru["wrist"].get("far_m") or 0) - 0.58) < 1e-6
    sm = s3.get("scene_mesh")
    assert isinstance(sm, dict) and "manifest" in sm
    vgm = s3.get("vis_geometry_markers_arm_m")
    assert isinstance(vgm, dict)
    for k in (
        "tag5_estimated_m",
        "arm_mount_m",
        "front_camera_from_tag5_m",
        "mjcf_depth_camera_m",
        "wrist_camera_display_m",
    ):
        assert k in vgm, k
    assert "go2_silhouette_anchor_arm_m" in s3
    assert "vis_geometry_chain_mm" in s3
    assert isinstance(s3.get("chain_order_plus_x"), dict)
    d1a = s3.get("d1_mesh_assets")
    assert isinstance(d1a, dict) and "looks_like_placeholder" in d1a
    assert isinstance(s3.get("viewer_3d_warnings"), list)

    r = client.get("/api/arm/scene_3d?fast=1")
    assert r.status_code == 200
    sf = r.get_json()
    assert sf.get("ok") is True
    assert sf.get("geometry_fast_preview") is True
    vsf = sf.get("vision_snapshot") or {}
    assert vsf.get("geometry_fast_preview") is True
    assert isinstance(sf.get("scene_graph"), dict)

    r = client.get("/api/arm/scene_meshes/go2/base_0.obj")
    assert r.status_code == 200
    r = client.get("/api/arm/scene_meshes/d1/base_link.STL")
    assert r.status_code == 200

    r = client.get("/api/mujoco/preview.png")
    assert r.status_code in (200, 503), (r.status_code, r.get_data(as_text=True)[:300])
    if r.status_code == 200:
        assert r.headers.get("Content-Type", "").startswith("image/png"), r.headers

    r = client.post("/api/base/accompany_mode", json={"enable": True, "mode": "joystick"})
    assert r.status_code == 403

    # Salvataggio START richiede GO2_LOCAL sulla NX
    r = client.post("/api/alignment/start_pose")
    assert r.status_code == 400

    r = client.post("/api/arm/teach_mode", json={"enable": True})
    assert r.status_code == 501
    assert r.get_json().get("reason") == "teach_drag_not_implemented"

    r = client.post("/api/arm/drag_follow", json={"enable": False})
    assert r.status_code == 200
    assert r.get_json().get("stopped") is True

    r = client.get("/api/arm/drag_follow")
    assert r.status_code == 200
    dj = r.get_json()
    assert dj.get("ok") is True
    assert dj.get("running") is False

    r = client.get("/api/arm/drag_follow/log")
    assert r.status_code == 200
    assert r.get_json().get("ok") is True

    r = client.get("/api/arm/drag_follow/diagnostics?servo=0")
    assert r.status_code == 200
    dj2 = r.get_json()
    assert dj2.get("ok") is True
    assert "summary" in dj2

    r = client.post("/api/arm/drag_follow", json={"enable": True})
    assert r.status_code == 403

    # Grasp: non avviare sequenza prima degli altri POST — il job può bloccare i giunti (409).
    r = client.get("/api/arm/job_status")
    assert r.status_code == 200
    job_j = r.get_json()
    assert job_j.get("ok") is True
    assert job_j.get("status") is not None

    r = client.get("/api/arm/true_zero")
    assert r.status_code == 200
    tz = r.get_json()
    assert tz.get("ok") is True
    assert "exists" in tz

    r = client.post("/api/arm/true_zero", json={"op": "save"})
    assert r.status_code == 400

    r = client.post("/api/arm/true_zero", json={"op": "goto_zero"})
    assert r.status_code == 503
    g0 = r.get_json()
    assert g0.get("skipped") is True
    assert g0.get("ok") is False

    r = client.post("/api/arm/joints/goto_deg", json={"servo_deg": [0, 0, 0, 0, 0, 0, 0]})
    assert r.status_code == 200
    gj = r.get_json()
    assert gj.get("skipped") is True

    r = client.post("/api/arm/joints/live_deg", json={"servo_deg": [0, 0, 0, 0, 0, 0, 0]})
    assert r.status_code == 200
    lv = r.get_json()
    assert lv.get("skipped") is True

    r = client.post("/api/arm/joints/move_one", json={"joint_index": 2, "angle_deg": 10.0})
    assert r.status_code in (200, 502)
    m1 = r.get_json()
    assert m1.get("skipped") is True or "feedback" in str(m1.get("reason", "")).lower()

    # Grasp POST: preflight asincrono → 202 immediato (dopo i POST giunti, così non ricevono 409).
    r = client.post("/api/arm/grasp_box/attempt", json={})
    assert r.status_code == 202, (r.status_code, r.get_data(as_text=True)[:400])
    att = r.get_json()
    assert att.get("ok") is True and att.get("accepted") is True and att.get("async_preflight") is True

    # --- Distanze slider ↔ catena (mm): formula pura + payload scene_3d con tag5 fittizio ---
    import json
    import math
    from unittest.mock import patch

    def _dist_mm(a: list[float], b: list[float]) -> float:
        return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3))) * 1000.0

    _t5 = [0.35, -0.02, 0.18]
    _av = (-0.20, 0.0, 0.0)
    _fv = (0.20, 0.0, -0.08)
    _mount = [_t5[0] + _av[0], _t5[1] + _av[1], _t5[2] + _av[2]]
    _front = [_t5[0] + _fv[0], _t5[1] + _fv[1], _t5[2] + _fv[2]]
    assert abs(_dist_mm(_t5, _mount) - 200.0) < 0.05, "tag5↔mount deve essere 200 mm (solo ΔX)"
    assert abs(_dist_mm(_t5, _front) - math.hypot(200.0, 80.0)) < 0.05, "tag5↔front default ΔX/ΔZ"
    _exp_mount_front_mm = math.hypot(400.0, 80.0)
    assert abs(_dist_mm(_mount, _front) - _exp_mount_front_mm) < 0.05, "mount↔front (Δ 400 mm X + 80 mm Z)"

    class _FakePlanResp:
        def get_data(self, as_text: bool = True) -> str:
            return json.dumps(
                {
                    "ok": True,
                    "selected": {"poses": {"ok": True, "poses": []}, "target": {}, "preview": {}},
                    "selected_camera": 6,
                }
            )

    def _fake_tag_estimates(_poses: dict) -> list:
        return [
            {
                "id": 5,
                "base_xyz_m": [0.35, -0.02, 0.18],
                "range_m": 0.5,
                "camera_xyz_m": [0.01, 0.02, 0.55],
            }
        ]

    with patch.object(dd, "api_box_plan", lambda: _FakePlanResp()), patch(
        "box_grasp_planner.apriltag_tag_estimates_base_m", _fake_tag_estimates
    ):
        with dd.VIS_GEOMETRY_TUNING_LOCK:
            dd.VIS_GEOMETRY_TUNING.clear()
        pl = dd._arm_scene_3d_payload()
        ch = pl.get("vis_geometry_chain_mm")
        assert ch is not None, "vis_geometry_chain_mm con tag5 sintetico"
        assert abs(float(ch["tag5_to_arm_mount_mm"]) - 200.0) < 0.2
        assert abs(float(ch["tag5_to_front_camera_model_mm"]) - math.hypot(200.0, 80.0)) < 0.2
        assert abs(float(ch["arm_mount_to_front_camera_model_mm"]) - _exp_mount_front_mm) < 0.2
        vm = pl["vis_geometry_markers_arm_m"]
        assert vm["arm_mount_m"][0] == 0.15 and vm["front_camera_from_tag5_m"][0] == 0.55

    pl_direct = dd._arm_scene_3d_payload()
    osc = pl_direct.get("mjcf_depth_optical_selfcheck_mm") or {}
    assert abs(float(osc.get("projection_on_optical_axis_mm", 0)) - 200.0) < 0.6
    assert abs(float(osc.get("chord_depth_to_nominal_mm", 0)) - 200.0) < 0.6
    _lm = pl_direct.get("viewer_landmarks_base_link_m") or {}
    assert isinstance(_lm.get("object_nominal_20cm_base_link_m"), list) and len(_lm["object_nominal_20cm_base_link_m"]) >= 3

    import numpy as np

    from box_grasp_planner import grip_point_from_object_detection, plan_from_frame
    from box_object_detector import detector_status

    blank = np.zeros((240, 320, 3), dtype=np.uint8)
    plan = plan_from_frame(blank)
    assert "tag_calibration" in plan
    assert "tags" in plan or isinstance(plan.get("tags"), dict)
    assert "grip_point" in plan and "object_detection" in plan
    tc = plan["tag_calibration"]
    assert tc.get("reference_tag_id") == 5
    assert tc.get("box_tag_edge_m") is not None
    fake_det = {
        "ok": True,
        "backend": "test",
        "confidence": 0.9,
        "bbox_xyxy": [80.0, 60.0, 220.0, 180.0],
        "bbox_center_px": [150.0, 120.0],
    }
    gp = grip_point_from_object_detection(fake_det, blank.shape[:2])
    assert gp.get("ok") is True and gp.get("gripper_model") == "east_west_close_to_center"
    ds = detector_status()
    assert ds.get("ok") is True and "recommended_models" in ds

    print("SMOKE_OK + vis_geometry mm chain verified (math + scene_3d payload)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print("SMOKE_FAIL:", exc, file=sys.stderr)
        raise SystemExit(1)

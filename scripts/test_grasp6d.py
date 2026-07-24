#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from go2_dashboard.d1_jog import grasp6d, pick_preset, pick_teach_model, program_store, service, wrist_rgbd
from go2_dashboard.d1_jog.app import create_d1_jog_app


class Grasp6DMathTests(unittest.TestCase):
    def test_aprilgrid_target_is_detected_from_multiple_tags(self) -> None:
        import cv2

        if not hasattr(cv2, "aruco") or not hasattr(cv2.aruco, "DICT_APRILTAG_36h11"):
            self.skipTest("OpenCV AprilTag dictionary unavailable")
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
        canvas = np.full((520, 760), 255, dtype=np.uint8)
        tag_px, gap_px = 80, 40
        for row in range(4):
            for col in range(6):
                tag_id = 288 + row * 6 + col
                marker = cv2.aruco.generateImageMarker(dictionary, tag_id, tag_px)
                y = 30 + row * (tag_px + gap_px)
                x = 30 + col * (tag_px + gap_px)
                canvas[y : y + tag_px, x : x + tag_px] = marker
        color = cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)
        intrinsics = {"fx": 800.0, "fy": 800.0, "ppx": 380.0, "ppy": 260.0, "coeffs": [0.0] * 5}
        out = grasp6d.detect_calibration_marker(color, intrinsics)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("target_type"), "aprilgrid_36h11")
        self.assertEqual(out.get("visible_marker_count"), 24)
        self.assertEqual(len(out.get("corners_px") or []), 96)
        self.assertLess(float(out.get("reprojection_rms_px", 99.0)), 0.5)

    def test_pose_ik_reconstructs_reachable_fk_pose(self) -> None:
        q = np.array([0.15, -1.0, 0.72, 0.18, 0.35, -0.2], dtype=float)
        target = grasp6d.fk_tool_transform(q)
        out = grasp6d.ik_pose(target, primary_seed=q + 0.03)
        self.assertTrue(out.get("ok"), out)
        self.assertLessEqual(float(out["position_error_m"]), 0.008)
        self.assertLessEqual(float(out["rotation_error_deg"]), 5.0)

    def test_floor_plane_ransac(self) -> None:
        rng = np.random.default_rng(4)
        x = rng.uniform(-0.4, 0.4, 1200)
        z = rng.uniform(0.25, 1.0, 1200)
        y = np.full_like(x, 0.24) + rng.normal(0.0, 0.002, len(x))
        floor = np.column_stack([x, y, z])
        clutter = rng.uniform([-0.15, -0.05, 0.35], [0.15, 0.18, 0.7], size=(180, 3))
        out = grasp6d.estimate_plane_ransac(np.vstack([floor, clutter]))
        self.assertTrue(out.get("ok"), out)
        normal = np.asarray(out["normal"], dtype=float)
        self.assertGreater(float(np.dot(normal, np.array([0.0, -1.0, 0.0]))), 0.97)

    def test_depth_stride_env_increases_depth_samples(self) -> None:
        depth = np.full((12, 12), 0.5, dtype=np.float32)
        intr = {"fx": 100.0, "fy": 100.0, "ppx": 6.0, "ppy": 6.0}
        with patch.dict("os.environ", {"D1_GRASP6D_DEPTH_STRIDE": "1"}):
            dense, _ = grasp6d.depth_to_points(depth, intr)
        with patch.dict("os.environ", {"D1_GRASP6D_DEPTH_STRIDE": "3"}):
            sparse, _ = grasp6d.depth_to_points(depth, intr)
        self.assertGreater(len(dense), len(sparse))
        self.assertEqual(len(dense), 144)

    def test_box_pose_from_floor_and_top_surface(self) -> None:
        height, width = 480, 640
        fx = fy = 400.0
        ppx, ppy = 320.0, 200.0
        depth = np.zeros((height, width), dtype=np.float32)
        floor_y = 0.28
        for v in range(201, height):
            z = floor_y * fy / (v - ppy)
            if 0.2 < z < 1.2:
                depth[v, :] = z
        top_y = 0.12
        for v in range(201, height):
            z = top_y * fy / (v - ppy)
            if not 0.45 <= z <= 0.65:
                continue
            u0 = int(round(ppx - 0.08 * fx / z))
            u1 = int(round(ppx + 0.08 * fx / z))
            depth[v, max(0, u0) : min(width, u1 + 1)] = z
        intr = {"fx": fx, "fy": fy, "ppx": ppx, "ppy": ppy}
        out = grasp6d.estimate_box_pose(depth, intr)
        self.assertTrue(out.get("ok"), out)
        dims = sorted(float(x) for x in out["dimensions_m"])
        self.assertGreater(dims[0], 0.10)
        self.assertLess(dims[-1], 0.25)

    def test_rgb_guided_box_pose_uses_detection_footprint(self) -> None:
        height, width = 240, 320
        fx = fy = 260.0
        ppx, ppy = 160.0, 90.0
        depth = np.zeros((height, width), dtype=np.float32)
        floor_y = 0.28
        for v in range(91, height):
            z = floor_y * fy / (v - ppy)
            if 0.2 < z < 1.2:
                depth[v, :] = z
        top_y = 0.12
        for v in range(91, height):
            z = top_y * fy / (v - ppy)
            if not 0.45 <= z <= 0.65:
                continue
            u0 = int(round(ppx - 0.08 * fx / z))
            u1 = int(round(ppx + 0.08 * fx / z))
            depth[v, max(0, u0) : min(width, u1 + 1)] = z
        intr = {"fx": fx, "fy": fy, "ppx": ppx, "ppy": ppy}
        top_corners_xyz = [
            (-0.08, top_y, 0.45),
            (0.08, top_y, 0.45),
            (0.08, top_y, 0.65),
            (-0.08, top_y, 0.65),
        ]
        orient_box_px = [
            [ppx + x * fx / z, ppy + y * fy / z]
            for x, y, z in top_corners_xyz
        ]
        det = {
            "ok": True,
            "bbox_xyxy": [125.0, 118.0, 195.0, 170.0],
            "bbox_center_px": [160.0, 144.0],
            "orient_box_px": orient_box_px,
            "backend": "test_rgb",
        }
        with patch.dict("os.environ", {"D1_GRASP6D_DEPTH_STRIDE": "1"}):
            out = grasp6d.estimate_box_pose_rgb_guided(depth, intr, det)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("source"), "rgb_guided_depth_sparse")
        self.assertAlmostEqual(float(out["dimensions_m"][0]), 0.20, delta=0.03)
        self.assertAlmostEqual(float(out["dimensions_m"][1]), 0.16, delta=0.03)
        self.assertGreater(int(out["point_count"]), 1000)

    def test_handeye_solver_recovers_consistent_transform(self) -> None:
        import cv2

        R_x, _ = cv2.Rodrigues(np.array([0.18, -0.09, 0.06], dtype=float))
        X = np.eye(4)
        X[:3, :3] = R_x
        X[:3, 3] = [0.035, -0.012, 0.028]
        base_target = np.eye(4)
        base_target[:3, 3] = [0.46, 0.04, 0.08]
        qs = [
            [0.00, -1.10, 0.75, 0.00, 0.25, 0.00],
            [0.25, -1.00, 0.65, 0.15, 0.35, -0.20],
            [-0.30, -0.90, 0.55, -0.20, 0.45, 0.25],
            [0.45, -0.75, 0.40, 0.30, 0.20, -0.35],
            [-0.50, -1.20, 0.85, -0.25, 0.15, 0.40],
            [0.15, -0.60, 0.30, 0.40, 0.55, -0.10],
            [-0.10, -1.30, 0.90, -0.35, 0.30, 0.15],
            [0.55, -0.95, 0.60, 0.10, 0.50, 0.30],
            [-0.42, -0.70, 0.35, 0.25, 0.60, -0.30],
            [0.32, -1.15, 0.82, -0.15, 0.18, 0.45],
        ]
        samples = []
        for q in qs:
            Tg = grasp6d.fk_tool_transform(q)
            Tc = np.linalg.inv(X) @ np.linalg.inv(Tg) @ base_target
            samples.append({"T_base_tool": Tg.tolist(), "T_camera_target": Tc.tolist()})
        with tempfile.TemporaryDirectory(prefix="handeye-") as tmp, patch.object(
            grasp6d, "CALIBRATION_PATH", Path(tmp) / "calibration.json"
        ):
            out = grasp6d.build_handeye_calibration(samples)
        self.assertTrue(out.get("ok"), out)
        recovered = np.asarray(out["T_tool_camera"], dtype=float)
        self.assertLess(float(np.linalg.norm(recovered[:3, 3] - X[:3, 3])), 0.005)

    def test_handeye_recomputes_legacy_fk_with_d1_encoder_signs(self) -> None:
        X = grasp6d._nominal_tool_camera_transform()
        base_target = np.eye(4)
        base_target[:3, 3] = [0.34, -0.22, 0.02]
        servo_poses_deg = [
            [-80, 10, 30, -20, 15, -25],
            [-65, 22, 18, 25, 38, 20],
            [-95, -5, 42, 12, -18, 35],
            [-50, 35, -8, -32, 26, -12],
            [-110, 18, 5, 38, -32, 28],
            [-72, -20, 35, 5, 45, -38],
        ]
        samples = []
        for servo_deg in servo_poses_deg:
            Tg = grasp6d.fk_tool_transform(np.radians(servo_deg))
            Tc = np.linalg.inv(X) @ np.linalg.inv(Tg) @ base_target
            samples.append(
                {
                    # Simula il T_base_tool legacy errato: deve essere ignorato
                    # quando sono disponibili gli encoder raw.
                    "T_base_tool": np.eye(4).tolist(),
                    "T_camera_target": Tc.tolist(),
                    "servo_deg": servo_deg + [5.0],
                }
            )
        out = grasp6d.solve_handeye_calibration(samples)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("kinematic_servo_signs"), [1, 1, 1, -1, 1, -1])
        self.assertTrue(out.get("physically_plausible"), out)
        recovered = np.asarray(out["T_tool_camera"], dtype=float)
        self.assertLess(float(np.linalg.norm(recovered[:3, 3] - X[:3, 3])), 0.005)

    def test_handeye_quality_does_not_report_ready_for_identical_poses(self) -> None:
        T_tool = np.eye(4)
        T_cam = np.eye(4)
        samples = [{"T_base_tool": T_tool.tolist(), "T_camera_target": T_cam.tolist()} for _ in range(8)]
        report = grasp6d.handeye_quality_report(samples)
        self.assertTrue(report.get("ok"), report)
        self.assertFalse(report.get("build_ready"), report)
        self.assertLess(int(report.get("progress_percent", 100)), 100)
        self.assertIn(report.get("next_action"), {"aumenta_diversita_pose", "residuo_alto_non_calcolare"})
        self.assertIn("sample_debug", report)
        self.assertIn("residual_trend", report)
        self.assertIn("max_translation_rms_m", report)
        self.assertIn("max_rotation_rms_deg", report)


class TeachCaptureTests(unittest.TestCase):
    def test_handeye_sample_holds_before_rgbd_capture(self) -> None:
        app = create_d1_jog_app()
        app.config.update(TESTING=True)
        events: list[str] = []
        feedback = {"ok": True, "servo_deg": [0.0, -40.0, 30.0, 0.0, 20.0, 0.0, 5.0]}
        frame = wrist_rgbd.WristRgbdFrame(
            color_bgr=np.zeros((24, 32, 3), dtype=np.uint8),
            depth_m=np.full((24, 32), 0.5, dtype=np.float32),
            intrinsics={"width": 32, "height": 24, "fx": 30.0, "fy": 30.0, "ppx": 16.0, "ppy": 12.0},
            serial="TEST",
            product_id="0b5c",
            depth_scale_m=0.001,
            timestamp_s=1.0,
        )
        marker = {
            "ok": True,
            "target_type": "aprilgrid_36h11",
            "pose_method": "tag_corners",
            "visible_marker_count": 8,
            "reprojection_rms_px": 0.2,
            "T_camera_target": np.eye(4).tolist(),
        }

        def hold_side_effect(*args, **kwargs):
            events.append("hold")
            return {"ok": True, "atomic_batch": True}

        def capture_side_effect(*args, **kwargs):
            events.append("capture")
            return frame

        with patch.object(service, "read_servo_deg", return_value=feedback), patch.object(
            service, "couple_and_hold_pose", side_effect=hold_side_effect
        ), patch.object(wrist_rgbd, "capture_aligned", side_effect=capture_side_effect), patch.object(
            grasp6d, "detect_calibration_marker", return_value=marker
        ), patch.object(grasp6d, "append_handeye_sample", return_value={"ok": True, "sample_count": 1}), patch.object(
            grasp6d, "record_calibration_event", return_value={}
        ):
            response = app.test_client().post("/api/pick/metric/calibration/sample", json={})

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(events[:2], ["hold", "capture"])

    def test_guided_2d_quality_requires_all_scenarios(self) -> None:
        samples = []
        for index, scenario in enumerate(pick_teach_model.GUIDED_SCENARIOS):
            samples.append(
                {
                    "scenario": scenario,
                    "vision_at_scan": {
                        "detected": True,
                        "norm": [0.2 + index * 0.07, 0.25 + index * 0.04],
                        "grip_align_deg": -30.0 + index * 8.0,
                    },
                }
            )
        quality = pick_teach_model.guided_quality_report(samples)
        self.assertTrue(quality["ready"], quality)
        self.assertEqual(quality["guided_count"], 8)
        samples.pop()
        self.assertFalse(pick_teach_model.guided_quality_report(samples)["ready"])

    def test_guided_2d_scenario_rejects_insufficient_box_displacement(self) -> None:
        center = {
            "scenario": "center",
            "vision_at_scan": {"detected": True, "norm": [0.5, 0.5], "grip_align_deg": 0.0},
        }
        too_close = {"detected": True, "norm": [0.46, 0.5], "grip_align_deg": 0.0}
        valid_left = {"detected": True, "norm": [0.35, 0.5], "grip_align_deg": 0.0}
        self.assertFalse(pick_teach_model.validate_guided_scenario("left", too_close, [center])["ok"])
        self.assertTrue(pick_teach_model.validate_guided_scenario("left", valid_left, [center])["ok"])

    def test_guided_2d_missing_detection_holds_but_does_not_persist(self) -> None:
        feedback = {"ok": True, "servo_deg": [0.0, -40.0, 30.0, 0.0, 20.0, 0.0, 50.0]}
        with tempfile.TemporaryDirectory(prefix="teach-guided-") as tmp, patch.object(
            pick_preset, "_PRESET_PATH", Path(tmp) / "preset.json"
        ), patch.object(service, "read_servo_deg", return_value=feedback), patch.object(
            pick_preset, "_couple_and_hold_taught_pose", return_value={"ok": True, "hold": {"ok": True}}
        ) as hold_mock:
            out = pick_teach_model.finish_teach_sample_after_release(
                vision_at_scan=None,
                scenario="center",
                require_valid_vision=True,
            )
            state = pick_teach_model.list_teach_samples()
        self.assertFalse(out.get("ok"), out)
        self.assertEqual(out.get("reason"), "fresh_2d_detection_required")
        self.assertEqual(state["count"], 0)
        hold_mock.assert_called_once()

    def test_handeye_sample_failure_still_restores_hold_and_tracks_event(self) -> None:
        app = create_d1_jog_app()
        app.config.update(TESTING=True)
        frame = wrist_rgbd.WristRgbdFrame(
            color_bgr=np.zeros((24, 32, 3), dtype=np.uint8),
            depth_m=np.full((24, 32), 0.5, dtype=np.float32),
            intrinsics={"width": 32, "height": 24, "fx": 30.0, "fy": 30.0, "ppx": 16.0, "ppy": 12.0},
            serial="TEST",
            product_id="0b5c",
            depth_scale_m=0.001,
            timestamp_s=1.0,
        )
        feedback = {"ok": True, "servo_deg": [0.0, -40.0, 30.0, 0.0, 20.0, 0.0, 50.0]}
        with tempfile.TemporaryDirectory(prefix="handeye-history-") as tmp, patch.object(
            grasp6d, "CALIBRATION_HISTORY_PATH", Path(tmp) / "history.json"
        ), patch.object(wrist_rgbd, "capture_aligned", return_value=frame), patch.object(
            grasp6d, "detect_calibration_marker", return_value={"ok": False, "reason": "calibration_target_not_found"}
        ), patch.object(service, "read_servo_deg", return_value=feedback), patch.object(
            service, "couple_and_hold_pose", return_value={"ok": True, "atomic_batch": True}
        ) as hold_mock:
            response = app.test_client().post("/api/pick/metric/calibration/sample", json={})
            history = grasp6d.calibration_history()
        self.assertEqual(response.status_code, 422)
        self.assertTrue(response.get_json()["hold"]["ok"])
        hold_mock.assert_called_once()
        self.assertEqual(history[-1]["event"], "sample_failed")

    def test_hold_now_forces_atomic_power_couple_even_when_flag_coupled(self) -> None:
        feedback = {"ok": True, "servo_deg": [0.0, -40.0, 30.0, 0.0, 20.0, 0.0, 50.0]}
        old_coupled = service._arm_coupled
        service._arm_coupled = True
        try:
            with patch.object(service, "read_servo_deg", return_value=feedback), patch.object(
                service, "couple_and_hold_pose", return_value={"ok": True, "atomic_batch": True}
            ) as atomic_hold, patch.object(service, "hold_pose_stream", return_value={"ok": True}) as pose_only:
                out = service.request_emergency_hold(reason="test")
        finally:
            service._arm_coupled = old_coupled
        self.assertTrue(out.get("ok"), out)
        atomic_hold.assert_called_once_with(
            feedback["servo_deg"], with_power=True, force=True, acquire_lock=False
        )
        pose_only.assert_not_called()

    def test_micro_jog_endpoint_rejects_large_delta(self) -> None:
        app = create_d1_jog_app()
        app.config.update(TESTING=True)
        response = app.test_client().post("/api/joints/micro_jog", json={"joint_index": 1, "delta_deg": 4.0})
        body = response.get_json()
        self.assertEqual(response.status_code, 422)
        self.assertFalse(body.get("ok"), body)
        self.assertEqual(body.get("reason"), "delta_out_of_range")

    def test_micro_jog_endpoint_holds_before_and_after_tiny_move(self) -> None:
        app = create_d1_jog_app()
        app.config.update(TESTING=True)
        feedback = {"ok": True, "servo_deg": [0.0, -40.0, 30.0, 0.0, 20.0, 0.0, 50.0]}
        with patch.object(service, "read_servo_deg", return_value=feedback), patch.object(
            service, "couple_and_hold_pose", return_value={"ok": True, "atomic_batch": True}
        ) as atomic_hold:
            response = app.test_client().post("/api/joints/micro_jog", json={"joint_index": 1, "delta_deg": 0.5})
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertTrue(body.get("ok"), body)
        self.assertEqual(atomic_hold.call_count, 2)
        self.assertEqual(body["target_servo_deg"][1], -39.5)

    def test_pregrasp_rejects_motion_outside_saved_scan_pose(self) -> None:
        app = create_d1_jog_app()
        app.config.update(TESTING=True)
        fallen_pose = {"ok": True, "servo_deg": [-78.4, 21.4, 6.9, -10.3, 88.9, 0.0, 5.4]}
        with tempfile.TemporaryDirectory(prefix="scan-pose-") as tmp, patch(
            "go2_dashboard.d1_jog.app._GRASP_SCAN_POSE_PATH", Path(tmp) / "scan.json"
        ), patch.object(service, "read_servo_deg", return_value=fallen_pose):
            response = app.test_client().post(
                "/api/pick/grasp6d/pregrasp",
                json={"require_offset_confirmation": False},
            )
        body = response.get_json()
        self.assertEqual(response.status_code, 422, body)
        self.assertEqual(body.get("reason"), "not_in_saved_scan_pose")
        self.assertFalse(body["scan_pose"]["aligned"])

    def test_execute_requires_selected_offset_confirmation(self) -> None:
        app = create_d1_jog_app()
        app.config.update(TESTING=True)
        with tempfile.TemporaryDirectory(prefix="grasp-bias-") as tmp, patch.object(
            grasp6d, "TUNING_PATH", Path(tmp) / "tuning.json"
        ):
            response = app.test_client().post(
                "/api/pick/grasp6d/execute",
                json={
                    "confirm": "EXECUTE_GRASP6D",
                    "require_offset_confirmation": True,
                    "expected_grasp_bias_base_m": [0.0, 0.0, 0.0],
                },
            )
        body = response.get_json()
        self.assertEqual(response.status_code, 409, body)
        self.assertEqual(body.get("reason"), "explicit_grasp_bias_confirmation_required")

    def test_absolute_grasp_bias_is_persisted(self) -> None:
        app = create_d1_jog_app()
        app.config.update(TESTING=True)
        with tempfile.TemporaryDirectory(prefix="grasp-bias-") as tmp, patch.object(
            grasp6d, "TUNING_PATH", Path(tmp) / "tuning.json"
        ):
            response = app.test_client().post(
                "/api/pick/grasp6d/tuning",
                json={"action": "set_grasp_bias", "grasp_bias_base_m": [0.01, -0.004, 0.002]},
            )
            saved = grasp6d.tuning_info()["values"]
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertAlmostEqual(saved["grasp_bias_base_x_m"], 0.01)
        self.assertAlmostEqual(saved["grasp_bias_base_y_m"], -0.004)
        self.assertAlmostEqual(saved["grasp_bias_base_z_m"], 0.002)

    def test_handeye_target_pdf_is_downloadable(self) -> None:
        app = create_d1_jog_app()
        app.config.update(TESTING=True)
        response = app.test_client().get("/api/pick/metric/calibration/target.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"%PDF-"))
        self.assertEqual(response.mimetype, "application/pdf")
        response.close()

    def test_debug_jpeg_contains_rgb_and_metric_cloud_panels(self) -> None:
        import cv2

        app = create_d1_jog_app()
        app.config.update(TESTING=True)
        frame = wrist_rgbd.WristRgbdFrame(
            color_bgr=np.zeros((120, 160, 3), dtype=np.uint8),
            depth_m=np.full((120, 160), 0.55, dtype=np.float32),
            intrinsics={"width": 160, "height": 120, "fx": 120.0, "fy": 120.0, "ppx": 80.0, "ppy": 60.0},
            serial="TEST",
            product_id="0b5c",
            depth_scale_m=0.001,
            timestamp_s=1.0,
        )
        box = {
            "ok": True,
            "center_camera_m": [0.02, 0.01, 0.55],
            "dimensions_m": [0.10, 0.08, 0.07],
            "point_count": 200,
            "height_threshold_m": {"min": 0.025, "default_min": 0.025, "max": 0.45},
            "sample_px_yx": [[60.0, 80.0]],
            "sample_height_m": [0.07],
            "components": [],
            "plane": {"ok": True, "normal": [0.0, -1.0, 0.0], "d": 0.01, "inlier_fraction": 0.8},
        }
        with patch.object(wrist_rgbd, "capture_aligned", return_value=frame), patch.object(
            grasp6d, "estimate_box_pose", return_value=box
        ), patch.object(grasp6d, "plan_grasp", return_value={"ok": False, "reason": "test"}), patch.object(
            service, "read_servo_deg", return_value={"ok": False}
        ):
            response = app.test_client().get("/api/pick/grasp6d/debug.jpg?capture=1")
        self.assertEqual(response.status_code, 200)
        image = cv2.imdecode(np.frombuffer(response.data, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(image.shape[:2], (120, 320))

    def test_teach_capture_holds_before_persisting(self) -> None:
        with tempfile.TemporaryDirectory(prefix="d1-programs-") as tmp:
            with patch.object(program_store, "_PROGRAMS_DIR", Path(tmp)):
                program = program_store.create_program("Teach")
                app = create_d1_jog_app()
                app.config.update(TESTING=True)
                feedback = {"ok": True, "servo_deg": [0.0, -40.0, 30.0, 0.0, 20.0, 0.0, 50.0]}
                hold = {"ok": True, "atomic_batch": True}
                with patch.object(service, "read_servo_deg", return_value=feedback), patch.object(
                    service, "couple_and_hold_pose", return_value=hold
                ) as hold_mock:
                    response = app.test_client().post(
                        f"/api/programs/{program['id']}/teach_capture", json={"name": "PRESA"}
                    )
                body = response.get_json()
                self.assertEqual(response.status_code, 200, body)
                self.assertTrue(body.get("ok"), body)
                self.assertEqual(body["waypoint"]["name"], "PRESA")
                hold_mock.assert_called_once()
                saved = program_store.load_program(program["id"])
                self.assertEqual(len(saved["waypoints"]), 1)

    def test_metric_preview_and_dry_run_never_move(self) -> None:
        app = create_d1_jog_app()
        app.config.update(TESTING=True)
        frame = wrist_rgbd.WristRgbdFrame(
            color_bgr=np.zeros((24, 32, 3), dtype=np.uint8),
            depth_m=np.full((24, 32), 0.5, dtype=np.float32),
            intrinsics={"width": 32, "height": 24, "fx": 30.0, "fy": 30.0, "ppx": 16.0, "ppy": 12.0},
            serial="TEST",
            product_id="0b5c",
            depth_scale_m=0.001,
            timestamp_s=1.0,
        )
        T = np.eye(4)
        T[:3, 3] = [0.4, 0.0, 0.12]
        box = {
            "ok": True,
            "T_camera_box": T,
            "center_camera_m": [0.0, 0.0, 0.5],
            "rotation_camera": np.eye(3).tolist(),
            "dimensions_m": [0.06, 0.09, 0.10],
            "point_count": 100,
            "plane": {"ok": True, "normal": [0.0, -1.0, 0.0]},
        }
        selected = {
            "T_base_grasp": T.tolist(),
            "T_base_pregrasp": T.tolist(),
            "pregrasp": {"ok": True, "servo_deg": [0, 0, 0, 0, 0, 0, 50]},
            "grasp": {"ok": True, "servo_deg": [0, 0, 0, 0, 0, 0, 50]},
        }
        plan = {"ok": True, "T_base_box": T.tolist(), "selected": selected, "candidate_count": 1}
        feedback = {"ok": True, "servo_deg": [0, 0, 0, 0, 0, 0, 50]}
        with patch.object(wrist_rgbd, "capture_aligned", return_value=frame), patch.object(
            grasp6d, "estimate_box_pose", return_value=box
        ), patch.object(grasp6d, "plan_grasp", return_value=plan), patch.object(
            service, "read_servo_deg", return_value=feedback
        ), patch("go2_dashboard.d1_jog.program_runner.move_to_servo_deg_smooth") as move_mock, patch(
            "go2_dashboard.d1_jog.app.PROJECT_ROOT", Path(tempfile.gettempdir()) / "d1-grasp6d-test"
        ):
            preview = app.test_client().post("/api/pick/metric/preview", json={})
            dry = app.test_client().post("/api/pick/grasp/goto", json={"dry_run": True})
        self.assertEqual(preview.status_code, 200, preview.get_json())
        self.assertEqual(dry.status_code, 200, dry.get_json())
        self.assertTrue(dry.get_json().get("ok"))
        move_mock.assert_not_called()

    def test_cluster_probe_checks_stability_without_motion(self) -> None:
        app = create_d1_jog_app()
        app.config.update(TESTING=True)
        frame = wrist_rgbd.WristRgbdFrame(
            color_bgr=np.zeros((24, 32, 3), dtype=np.uint8),
            depth_m=np.full((24, 32), 0.5, dtype=np.float32),
            intrinsics={"width": 32, "height": 24, "fx": 30.0, "fy": 30.0, "ppx": 16.0, "ppy": 12.0},
            serial="TEST",
            product_id="0b5c",
            depth_scale_m=0.001,
            timestamp_s=1.0,
        )
        T = np.eye(4)
        T[:3, 3] = [0.4, 0.0, 0.12]
        box = {
            "ok": True,
            "T_camera_box": T,
            "center_camera_m": [0.0, 0.0, 0.5],
            "rotation_camera": np.eye(3).tolist(),
            "dimensions_m": [0.06, 0.09, 0.10],
            "point_count": 120,
            "plane": {"ok": True, "normal": [0.0, -1.0, 0.0]},
        }
        selected = {
            "T_base_grasp": T.tolist(),
            "T_base_pregrasp": T.tolist(),
            "pregrasp": {"ok": True, "servo_deg": [0, 0, 0, 0, 0, 0, 50]},
            "grasp": {"ok": True, "servo_deg": [0, 0, 0, 0, 0, 0, 50]},
        }
        plan = {"ok": True, "T_base_box": T.tolist(), "selected": selected, "candidate_count": 1}
        feedback = {"ok": True, "servo_deg": [0, 0, 0, 0, 0, 0, 50]}
        with patch.object(wrist_rgbd, "capture_aligned", return_value=frame), patch.object(
            grasp6d, "estimate_box_pose", return_value=box
        ), patch.object(grasp6d, "plan_grasp", return_value=plan), patch.object(
            service, "read_servo_deg", return_value=feedback
        ), patch("go2_dashboard.d1_jog.program_runner.move_to_servo_deg_smooth") as move_mock, patch(
            "go2_dashboard.d1_jog.app.PROJECT_ROOT", Path(tempfile.gettempdir()) / "d1-grasp6d-test"
        ):
            response = app.test_client().post(
                "/api/pick/grasp6d/cluster_probe",
                json={"frames": 5, "interval_s": 0.0},
            )
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertTrue(body.get("ok"), body)
        self.assertEqual(body["summary"]["valid_observations"], 5)
        self.assertTrue(body["summary"]["ready_for_pregrasp"])
        self.assertFalse(body["safety"]["moves_arm"])
        move_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

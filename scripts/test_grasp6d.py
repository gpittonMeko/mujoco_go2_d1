#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from go2_dashboard.d1_jog import grasp6d, program_store, service, wrist_rgbd
from go2_dashboard.d1_jog.app import create_d1_jog_app


class Grasp6DMathTests(unittest.TestCase):
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


class TeachCaptureTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

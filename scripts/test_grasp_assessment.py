#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from go2_dashboard.grasp_assessment import candidate_grasp_assessment, detector_training_scope, plan_grasp_assessment
from box_grasp_planner import plan_from_frame
import diagnostics_dashboard as dd


class GraspAssessmentTests(unittest.TestCase):
    def test_candidate_with_box_tags_is_validated_3d(self) -> None:
        cand = {
            "absolute_ik_safe": True,
            "tags": {"tags": [{"id": 0}, {"id": 2}]},
            "target": {"ok": True},
            "preview": {"ok": True, "plan": [{"stage": "grasp"}]},
            "grip_point": {"ok": True, "source": "apriltag"},
            "object_detection": {"ok": True, "backend": "apriltag"},
        }
        out = candidate_grasp_assessment(cand)
        self.assertEqual(out["tier"], "validated_3d_grasp_candidate")
        self.assertTrue(out["validated_3d"])
        self.assertTrue(out["execution_allowed"])
        self.assertEqual(out["source_kind"], "apriltag_box_pose")

    def test_candidate_with_box_tags_but_no_preview_is_still_3d_observation(self) -> None:
        cand = {
            "absolute_ik_safe": True,
            "tags": {"tags": [{"id": 0}]},
            "target": {"ok": True},
            "preview": {"ok": False, "plan": []},
            "grip_point": {"ok": True, "source": "apriltag"},
            "object_detection": {"ok": True, "backend": "apriltag"},
        }
        out = candidate_grasp_assessment(cand)
        self.assertEqual(out["tier"], "validated_3d_observation")
        self.assertTrue(out["validated_source_3d"])
        self.assertFalse(out["preview_only"])
        self.assertFalse(out["execution_allowed"])

    def test_candidate_without_tags_is_preview_only_by_default(self) -> None:
        cand = {
            "absolute_ik_safe": True,
            "tags": {"tags": []},
            "target": {"ok": True},
            "preview": {"ok": True, "plan": [{"stage": "grasp"}]},
            "grip_point": {"ok": True, "source": "classic_contour_fallback"},
            "object_detection": {"ok": True, "backend": "classic_contour_fallback"},
        }
        with patch.dict(os.environ, {"GO2_GRASP_ALLOW_HEURISTIC_EXECUTE": "0"}, clear=False):
            out = candidate_grasp_assessment(cand)
        self.assertEqual(out["tier"], "heuristic_preview_only")
        self.assertTrue(out["preview_only"])
        self.assertFalse(out["execution_allowed"])
        self.assertIn("object_pose_not_validated_3d", out["warnings"])

    def test_plan_from_frame_uses_depth_roi_as_validated_3d_source(self) -> None:
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        frame[:, :, 1] = 40
        frame[:, :, 2] = 12
        depth = np.full((240, 320), 420, dtype=np.uint16)
        depth[80:170, 100:220] = 380
        fake_det = {
            "ok": True,
            "backend": "yolo11n",
            "confidence": 0.93,
            "bbox_xyxy": [96.0, 74.0, 224.0, 178.0],
            "bbox_center_px": [160.0, 126.0],
            "grip_center_px": [160.0, 126.0],
            "grip_axis_px": [[96.0, 126.0], [224.0, 126.0]],
        }
        plan = plan_from_frame(frame, object_detection=fake_det, depth_frame=depth, logical_camera_device=6)
        self.assertTrue(plan["ok"])
        self.assertEqual((plan.get("object_detection") or {}).get("backend"), "rgbd_depth_fused")
        target = plan.get("target") or {}
        self.assertTrue(target.get("ok"))
        self.assertEqual(target.get("source"), "rgbd_depth_fused")
        self.assertGreater(float(target.get("depth_support_fraction") or 0.0), 0.0)
        assess = candidate_grasp_assessment(plan)
        self.assertEqual(assess["tier"], "validated_3d_grasp_candidate")
        self.assertTrue(assess["execution_allowed"])
        self.assertEqual(assess["source_kind"], "depth_3d_estimate")

    def test_heuristic_execute_override_is_explicit(self) -> None:
        cand = {
            "absolute_ik_safe": True,
            "tags": {"tags": []},
            "target": {"ok": True},
            "preview": {"ok": True, "plan": [{"stage": "grasp"}]},
            "grip_point": {"ok": True, "source": "classic_contour_fallback"},
            "object_detection": {"ok": True, "backend": "classic_contour_fallback"},
        }
        with patch.dict(os.environ, {"GO2_GRASP_ALLOW_HEURISTIC_EXECUTE": "1"}, clear=False):
            out = candidate_grasp_assessment(cand)
        self.assertTrue(out["execution_allowed"])
        self.assertIn("heuristic_execution_override_enabled", out["warnings"])

    def test_plan_assessment_reports_selected_execution_gate(self) -> None:
        plan = {
            "ok": True,
            "selected_camera": 6,
            "candidates": {
                "0": {"tags": {"tags": []}, "target": {"ok": False}, "preview": {"ok": False}},
                "6": {
                    "absolute_ik_safe": True,
                    "tags": {"tags": [{"id": 1}]},
                    "target": {"ok": True},
                    "preview": {"ok": True, "plan": [{"stage": "grasp"}]},
                },
            },
        }
        out = plan_grasp_assessment(plan)
        self.assertTrue(out["has_validated_3d_any"])
        self.assertTrue(out["selected_execution_allowed"])
        self.assertEqual(out["selected"]["tier"], "validated_3d_grasp_candidate")

    def test_detector_training_scope_closed_set_labels(self) -> None:
        out = detector_training_scope(
            {
                "model_family": "ultralytics_pt",
                "model_exists": True,
                "trained_labels": ["box", "bottle"],
                "training_scope": "closed_set_labels",
                "open_vocabulary": False,
                "recommended_use_it": "closed set",
            }
        )
        self.assertEqual(out["training_scope"], "closed_set_labels")
        self.assertEqual(out["trained_label_count"], 2)
        self.assertIn("closed-set", out["label_it"])

    def test_publish_d1_arm_plan_blocks_heuristic_preview_without_override(self) -> None:
        payload = {
            "ok": True,
            "selected_camera": 6,
            "selected": {
                "ok": True,
                "absolute_ik_safe": True,
                "tags": {"tags": []},
                "target": {"ok": True},
                "preview": {"ok": True, "plan": [{"stage": "grasp", "joints_rad": [0, 0, 0, 0, 0, 0]}]},
                "grip_point": {"ok": True, "source": "classic_contour_fallback"},
                "object_detection": {"ok": True, "backend": "classic_contour_fallback"},
            },
        }
        with patch.dict(os.environ, {"GO2_ENABLE_REAL_ARM": "1", "GO2_GRASP_ALLOW_HEURISTIC_EXECUTE": "0"}, clear=False):
            out = dd.publish_d1_arm_plan(payload)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "selected_candidate_not_allowed_for_execute")
        self.assertFalse(out["attempted_motion"])


if __name__ == "__main__":
    unittest.main()

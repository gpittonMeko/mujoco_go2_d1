#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from go2_dashboard import d1_arm_motion
from go2_dashboard.d1_jog import service


class D1HoldServiceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        service.motion_force_idle()
        service._arm_coupled = False
        service.set_servo_cache([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 30.0])

    def test_power_couple_uses_one_external_writer_and_includes_pose(self) -> None:
        state = {"active": False}

        def status(*args: object, **kwargs: object) -> dict[str, object]:
            return {
                "ok": True,
                "publisher_alive": state["active"],
                "desired_coupled": state["active"],
                "hold_active": state["active"],
            }

        captured: list[dict[str, object]] = []

        def publish(messages: list[dict[str, object]], **kwargs: object) -> dict[str, object]:
            captured.extend(messages)
            state["active"] = True
            return {"ok": True, "count": len(messages), "hold_active": True}

        with patch.dict(
            os.environ,
            {
                "GO2_ENABLE_REAL_ARM": "1",
                "D1_HOLD_DAEMON_EXTERNAL": "1",
            },
            clear=False,
        ), patch.object(
            service, "_fresh_pose_for_safe_hold", return_value=[0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 30.0]
        ), patch.object(service.d1_hold_client, "status", side_effect=status), patch.object(
            service.d1_hold_client, "publish", side_effect=publish
        ):
            result = service.ensure_coupled(with_power=True, force=True)

        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("arm_coupled"), result)
        self.assertEqual([int(m["funcode"]) for m in captured], [6, 5, 2])

    def test_servo_feedback_does_not_mark_torque_active_by_default(self) -> None:
        service._arm_coupled = False
        with patch.dict(os.environ, {"D1_INFER_COUPLED_ON_FEEDBACK": "0"}, clear=False):
            self.assertFalse(service.mark_coupled_from_feedback())
        self.assertFalse(service._arm_coupled)

    def test_kill_is_blocked_when_external_hold_owns_writer(self) -> None:
        with patch.dict(os.environ, {"D1_HOLD_DAEMON_EXTERNAL": "1"}, clear=False):
            result = d1_arm_motion.kill_command_processes()
        self.assertTrue(result.get("safety_interlock"), result)
        self.assertTrue(result.get("skipped"), result)

    def test_funcode7_zero_is_blocked_under_continuous_hold(self) -> None:
        with patch.dict(os.environ, {"D1_HOLD_DAEMON_EXTERNAL": "1"}, clear=False):
            result = service.go_zero()
        self.assertFalse(result.get("ok"), result)
        self.assertTrue(result.get("safety_interlock"), result)

    def test_safe_couple_never_falls_back_to_stale_cache(self) -> None:
        service.set_servo_cache([10.0] * 7)
        with patch.object(service, "read_servo_deg", return_value={"ok": False, "reason": "timeout"}):
            self.assertIsNone(service._fresh_pose_for_safe_hold())


if __name__ == "__main__":
    unittest.main()

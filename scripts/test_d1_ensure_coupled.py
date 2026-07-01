#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path
import sys
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from go2_dashboard.d1_jog import service


class EnsureCoupledForMotionTests(unittest.TestCase):
    def test_funcode5_is_forced_before_every_motion_by_default(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GO2_ENFORCE_FUNCODE5_BEFORE_MOTION": "1",
                "GO2_ENFORCE_POWER_BEFORE_MOTION": "1",
            },
            clear=False,
        ), patch.object(
            service,
            "ensure_coupled",
            return_value={"ok": True, "action": "ensure_coupled"},
        ) as ensure:
            out = service.ensure_coupled_for_motion()

        ensure.assert_called_once_with(with_power=True, force=True)
        self.assertTrue(out["ok"])
        self.assertTrue(out["forced_couple"])

    def test_funcode5_does_not_self_deadlock_inside_program_lock(self) -> None:
        old_coupled = service._arm_coupled
        service.motion_force_idle()
        service._arm_coupled = True
        acquired, reason = service.motion_try_acquire("program")
        self.assertTrue(acquired, reason)
        try:
            with patch.dict(
                "os.environ",
                {"GO2_ENFORCE_FUNCODE5_BEFORE_MOTION": "1"},
                clear=False,
            ), patch.object(
                service,
                "arm_couple_once",
                return_value={"ok": True, "action": "arm_couple_once"},
            ) as couple, patch.object(service, "ensure_coupled") as admin_ensure:
                out = service.ensure_coupled_for_motion()
        finally:
            service.motion_release("program")
            service._arm_coupled = old_coupled

        couple.assert_called_once_with(force=True)
        admin_ensure.assert_not_called()
        self.assertTrue(out["ok"])
        self.assertEqual(out["motion_context"], "program")
        self.assertTrue(out["admin_lock_skipped"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class MotionPathGuardSourceTests(unittest.TestCase):
    def test_operator_arm_session_does_not_use_plain_ensure_coupled(self) -> None:
        src = (REPO_ROOT / "go2_dashboard" / "operator_arm_motion.py").read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("ensure_coupled(with_power=False)", src)
        self.assertIn("ensure_coupled_for_motion()", src)

    def test_d1_arm_motion_wrapper_delegates_to_jog_service(self) -> None:
        src = (REPO_ROOT / "go2_dashboard" / "d1_arm_motion.py").read_text(encoding="utf-8", errors="replace")
        self.assertIn("return jog_svc.ensure_coupled_for_motion()", src)
        self.assertNotIn("return jog_svc.arm_couple_once(force=False)", src)

    def test_repo_rule_mentions_persistent_daemon_and_hold_stream(self) -> None:
        rule = (REPO_ROOT / ".cursor" / "rules" / "d1-arm-funcode-hold.mdc").read_text(
            encoding="utf-8",
            errors="replace",
        )
        self.assertIn("un solo daemon persistente", rule)
        self.assertIn("hold_pose_stream", rule)
        self.assertIn("scripts/d1_hold_daemon.py", rule)

    def test_agents_contains_non_negotiable_external_hold_invariant(self) -> None:
        agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8", errors="replace")
        self.assertIn("sole owner of `bin/d1_sdk_command`", agents)
        self.assertIn("D1_INFER_COUPLED_ON_FEEDBACK=0", agents)
        self.assertIn("Dashboard restarts must leave the hold daemon PID", agents)

    def test_nx_deploy_enables_external_hold_and_disables_feedback_inference(self) -> None:
        deploy = (REPO_ROOT / "scripts" / "deploy_dashboard_to_nx.py").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("export D1_HOLD_DAEMON_EXTERNAL=1", deploy)
        self.assertIn("export D1_INFER_COUPLED_ON_FEEDBACK=0", deploy)
        self.assertIn("_run_d1_hold_safety_gate()", deploy)
        self.assertIn("REFUSE_DEPLOY_D1_SAFETY_TEST_FAILED", deploy)

    def test_kill_path_has_external_owner_interlock(self) -> None:
        src = (REPO_ROOT / "go2_dashboard" / "d1_arm_motion.py").read_text(
            encoding="utf-8", errors="replace"
        )
        interlock = src.index("external_hold_daemon_owns_writer")
        pkill = src.index('["pkill", "-f", pat]')
        self.assertLess(interlock, pkill)

    def test_daemon_contains_autonomous_heartbeat(self) -> None:
        src = (REPO_ROOT / "scripts" / "d1_hold_daemon.py").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("def _heartbeat_loop", src)
        self.assertIn('source="heartbeat"', src)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent


class D1RgbAndMotorUiTests(unittest.TestCase):
    def test_nx_env_pins_color_nodes_not_depth_nodes(self) -> None:
        src = (ROOT / "scripts" / "nx_d1_jog_env.sh").read_text(encoding="utf-8", errors="replace")
        self.assertIn('D1_WRIST_V4L_INDEX="${D1_WRIST_V4L_INDEX:-4}"', src)
        self.assertIn('D1_FRONT_V4L_INDEX="${D1_FRONT_V4L_INDEX:-10}"', src)
        self.assertIn('D1_ORBBEC_RGB_V4L_INDEX="${D1_ORBBEC_RGB_V4L_INDEX:-4}"', src)
        self.assertIn('D1_ORBBEC_RELOAD_UVC="${D1_ORBBEC_RELOAD_UVC:-0}"', src)
        self.assertNotIn('D1_WRIST_V4L_INDEX="${D1_WRIST_V4L_INDEX:-2}"', src)
        self.assertNotIn('D1_FRONT_V4L_INDEX="${D1_FRONT_V4L_INDEX:-8}"', src)

    def test_app_resolves_both_realsense_color_interfaces(self) -> None:
        src = (ROOT / "go2_dashboard" / "d1_jog" / "app.py").read_text(encoding="utf-8", errors="replace")
        self.assertIn('"D1_WRIST_V4L_INDEX", "0b5c", 4', src)
        self.assertIn('"D1_FRONT_V4L_INDEX", "0b3a", 10', src)
        self.assertIn('return ":1.3" in device_path and stream_index == 0', src)
        self.assertIn('"detection_source": "wrist"', src)
        self.assertIn('for i in range(12):', src)
        self.assertIn('move_to_servo_deg_smooth(target)', src)
        self.assertIn('out["sport_probe"] = invoke_dds_sport_ping()', src)
        self.assertIn('@app.route("/assets/<string:name>")', src)

    def test_ui_has_rgb_badges_and_full_motor_panel(self) -> None:
        src = (ROOT / "templates" / "d1_jog_dashboard.html").read_text(encoding="utf-8", errors="replace")
        self.assertIn('id="wristRgbBadge"', src)
        self.assertIn('id="frontRgbBadge"', src)
        self.assertIn('src="/motors"', src)
        self.assertIn('Unitree', src)
        self.assertIn('id="loadingScreen"', src)
        self.assertIn('id="diagJson"', src)
        self.assertIn('id="scanResult"', src)
        self.assertIn('id="scanPreview"', src)
        self.assertIn("function renderScanResult(payload)", src)
        self.assertIn('/assets/unitree_go2_hero.png', src)
        self.assertIn('id="btnRelease"', src)
        self.assertIn('id="btnHoldNow"', src)
        self.assertIn('id="btnSaveZeroPose"', src)
        self.assertIn('id="btnGotoZeroPose"', src)
        self.assertIn('id="teachProgramSelect"', src)
        self.assertIn('id="btnSavePose"', src)
        self.assertIn('Memorizza posa + HOLD', src)
        self.assertIn('id="teachZeroStatus"', src)
        self.assertIn('window.confirm(', src)
        self.assertIn("{ confirm: 'RELEASE_ARM_TORQUE' }", src)
        self.assertNotIn('id="btnSavePickTuning"', src)
        self.assertNotIn('id="btnSaveGraspParams"', src)
        self.assertNotIn('Tuning avanzato presa', src)
        self.assertNotIn('Parametri calibrazione presa', src)
        self.assertIn('/teach_capture', src)
        self.assertNotIn('id="heroCardA"', src)
        self.assertNotIn('}, 45000);', src)

    def test_historical_motor_modules_are_present(self) -> None:
        for rel in (
            "go2_dashboard/motor_health_app.py",
            "go2_dashboard/go2_motor_health.py",
            "go2_dashboard/go2_motor_event_log.py",
            "go2_dashboard/go2_motor_sport.py",
            "go2_dashboard/go2_thermal_protect.py",
            "go2_dashboard/go2_thermal_settings.py",
        ):
            self.assertTrue((ROOT / rel).is_file(), rel)

    def test_arm_runtime_and_daemon_regressions_are_guarded(self) -> None:
        env = (ROOT / "scripts" / "nx_d1_jog_env.sh").read_text(encoding="utf-8", errors="replace")
        service = (ROOT / "go2_dashboard" / "d1_jog" / "service.py").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertEqual(env.count("D1_JOG_ENABLE_EVERY_TICKS="), 1)
        self.assertIn("D1_JOG_ENABLE_EVERY_TICKS=0", env)
        self.assertEqual(env.count("D1_ORBBEC_RGB_ONLY="), 1)
        self.assertIn("sdk_reinstall_backup_19700225_160102", env)
        self.assertIn("unset LD_PRELOAD", env)
        self.assertIn("def command_daemon_status()", service)
        self.assertIn('"daemon_died_after_publish"', service)
        self.assertIn("def couple_and_hold_pose(", service)
        self.assertIn('out["atomic_batch"] = True', service)
        self.assertNotIn("arm_keeper", service)
        self.assertIn("ensure_coupled(with_power=False, force=False)", service)
        self.assertNotIn("ensure_coupled(with_power=True, force=True)", service)
        self.assertIn("def request_emergency_hold(", service)
        self.assertIn("def safe_zero_pose_from_servo(", service)
        self.assertIn('D1_ZERO_TRANSIT_J1_DEG", "-90"', service)
        self.assertIn("d1_hold_client.external_hold_enabled()", service)
        self.assertIn('"continuous_hold_active"', service)
        self.assertIn('TRUE_ZERO_POSE_PATH = PROJECT_ROOT / "data" / "true_zero_pose.json"', service)
        app = (ROOT / "go2_dashboard" / "d1_jog" / "app.py").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn('body.get("confirm") != "RELEASE_ARM_TORQUE"', app)
        self.assertIn('"explicit_release_confirmation_required"', app)
        self.assertIn('@app.route("/api/joints/hold_now"', app)
        self.assertIn('@app.route("/api/arm/true_zero"', app)
        self.assertIn('@app.route("/api/programs/<program_id>/teach_capture"', app)
        self.assertIn('service.couple_and_hold_pose(taught, with_power=True, force=True)', app)
        self.assertIn('service.safe_zero_pose_from_servo', app)
        self.assertIn('side_transit[0] = float(target[0])', app)
        self.assertIn('"rotate_while_folded"', app)
        self.assertIn('"safe_transit_unavailable"', app)
        jog_stream = (ROOT / "go2_dashboard" / "d1_jog" / "jog_stream.py").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertNotIn("publish_messages_stream(msgs", jog_stream)
        start = (ROOT / "scripts" / "nx_start_d1_jog.sh").read_text(encoding="utf-8", errors="replace")
        self.assertIn("command_daemon", start)
        self.assertIn("startup_arm_stabilization", start)
        self.assertIn("nx_start_d1_hold_daemon.sh", start)
        self.assertIn("hold_active", start)
        self.assertTrue((ROOT / "scripts" / "verify_d1_arm_stack.py").is_file())

    def test_d1_boot_autostart_assets_are_present(self) -> None:
        deploy = (ROOT / "scripts" / "deploy_d1_jog_to_nx.py").read_text(encoding="utf-8", errors="replace")
        boot = (ROOT / "scripts" / "nx_boot_d1_jog_wrapper.sh").read_text(encoding="utf-8", errors="replace")
        svc = (ROOT / "scripts" / "go2-d1-jog-dashboard.service").read_text(encoding="utf-8", errors="replace")
        fg = (ROOT / "scripts" / "nx_serve_foreground_d1_jog.sh").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("scripts/nx_boot_d1_jog_wrapper.sh", deploy)
        self.assertIn("scripts/go2-d1-jog-dashboard.service", deploy)
        self.assertIn("_remote_install_d1_crontab", deploy)
        self.assertIn("_remote_install_d1_systemd_user_optional", deploy)
        self.assertIn("_remote_remove_legacy_5052_autostart", deploy)
        self.assertIn("GO2_DASHBOARD_AUTOSTART", deploy)
        self.assertIn("nx_boot_dashboard_wrapper.sh", deploy)
        self.assertIn("GO2_D1_JOG_AUTOSTART", boot)
        self.assertIn("bash scripts/nx_start_d1_jog.sh", boot)
        self.assertIn("ExecStart=/home/unitree/go2_visual_dashboard/scripts/nx_serve_foreground_d1_jog.sh", svc)
        self.assertIn("source /home/unitree/go2_visual_dashboard/scripts/nx_d1_jog_env.sh", fg)
        self.assertIn("serve_d1_jog_dashboard.py", fg)

    def test_hermes_is_integrated_on_5056(self) -> None:
        app = (ROOT / "go2_dashboard" / "d1_jog" / "app.py").read_text(encoding="utf-8", errors="replace")
        ui = (ROOT / "templates" / "d1_jog_dashboard.html").read_text(encoding="utf-8", errors="replace")
        context = (ROOT / "go2_dashboard" / "hermes" / "context.py").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn('app.register_blueprint(hermes_bp)', app)
        self.assertIn('id="tabHermesBtn"', ui)
        self.assertIn('data-src="/focus/hermes"', ui)
        self.assertIn("http://127.0.0.1:5056", context)
        self.assertIn('integrato nello stesso processo e sulla stessa porta 5056', ui)
        hermes_html = (ROOT / "templates" / "hermes.html").read_text(encoding="utf-8")
        hermes_css = (ROOT / "static" / "hermes" / "hermes.css").read_text(encoding="utf-8")
        hermes_js = (ROOT / "static" / "hermes" / "hermes.js").read_text(encoding="utf-8")
        self.assertIn('/static/hermes/hermes.css', hermes_html)
        self.assertIn('.msn-window', hermes_css)
        self.assertIn('integrato nella dashboard 5056', hermes_js)


if __name__ == "__main__":
    unittest.main()

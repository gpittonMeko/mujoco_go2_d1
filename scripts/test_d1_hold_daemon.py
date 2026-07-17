#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from go2_dashboard import d1_hold_client


class D1HoldDaemonIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="d1hold-")
        root = Path(self.tmp.name)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        self.socket = f"tcp://127.0.0.1:{port}"
        self.state = root / "state.json"
        self.lock = root / "daemon.lock"
        self.log = root / "publisher.ndjson"
        self.proc: subprocess.Popen[str] | None = None
        self.env = {
            "D1_HOLD_DAEMON_EXTERNAL": "1",
            "D1_HOLD_SOCKET": self.socket,
        }

    def tearDown(self) -> None:
        self._stop()
        self.tmp.cleanup()

    def _start(self) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "d1_hold_daemon.py"),
                "--socket",
                self.socket,
                "--state",
                str(self.state),
                "--lock",
                str(self.lock),
                "--heartbeat-ms",
                "40",
                "--fake-log",
                str(self.log),
            ],
            cwd=str(REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 5.0
        with patch.dict(os.environ, self.env, clear=False):
            while time.time() < deadline:
                if d1_hold_client.status(timeout_s=0.2).get("ok"):
                    return
                time.sleep(0.03)
        self.fail("hold daemon did not create its socket")

    def _stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=4.0)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=2.0)
        if self.proc.stderr is not None:
            self.proc.stderr.close()
        self.proc = None

    @staticmethod
    def _pose(seq: int = 3) -> dict[str, object]:
        data: dict[str, object] = {"mode": 1}
        for index in range(7):
            data[f"angle{index}"] = float(index)
        return {"seq": seq, "address": 1, "funcode": 2, "data": data}

    def test_heartbeat_survives_dashboard_client_disconnect(self) -> None:
        self._start()
        messages = [
            {"seq": 1, "address": 1, "funcode": 6, "data": {"power": 1}},
            {"seq": 2, "address": 1, "funcode": 5, "data": {"mode": 1}},
            self._pose(),
        ]
        with patch.dict(os.environ, self.env, clear=False):
            published = d1_hold_client.publish(messages)
            self.assertTrue(published.get("ok"), published)
            first_pid = published.get("daemon_pid")
            time.sleep(0.2)
            first = d1_hold_client.status()
            self.assertTrue(first.get("hold_active"), first)
            first_count = int(first.get("heartbeat_count", 0))

            # Every request uses a fresh connection, equivalent to the Flask
            # process disappearing and a new dashboard client connecting.
            time.sleep(0.2)
            second = d1_hold_client.status()
            self.assertEqual(second.get("daemon_pid"), first_pid)
            self.assertGreater(int(second.get("heartbeat_count", 0)), first_count)
            self.assertTrue(second.get("hold_active"), second)
            self.assertEqual(second.get("hold_target_servo_deg"), [float(i) for i in range(7)])
            events = second.get("recent_events")
            self.assertIsInstance(events, list)
            self.assertTrue(any(row.get("source") == "heartbeat" for row in events), events)

    def test_daemon_restart_same_boot_restores_power_couple_and_pose(self) -> None:
        self._start()
        messages = [
            {"seq": 11, "address": 1, "funcode": 6, "data": {"power": 1}},
            {"seq": 12, "address": 1, "funcode": 5, "data": {"mode": 1}},
            self._pose(13),
        ]
        with patch.dict(os.environ, self.env, clear=False):
            self.assertTrue(d1_hold_client.publish(messages).get("ok"))
            time.sleep(0.12)
        self._stop()
        self._start()
        with patch.dict(os.environ, self.env, clear=False):
            deadline = time.time() + 2.0
            status: dict[str, object] = {}
            while time.time() < deadline:
                status = d1_hold_client.status()
                if status.get("hold_active"):
                    break
                time.sleep(0.03)
            self.assertTrue(status.get("hold_active"), status)
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        restored = [row["message"].get("funcode") for row in rows if row.get("source") == "restore"]
        self.assertIn(6, restored)
        self.assertIn(5, restored)
        self.assertIn(2, restored)

    def test_release_stops_heartbeat_without_killing_daemon(self) -> None:
        self._start()
        with patch.dict(os.environ, self.env, clear=False):
            self.assertTrue(
                d1_hold_client.publish(
                    [
                        {"seq": 21, "address": 1, "funcode": 5, "data": {"mode": 1}},
                        self._pose(22),
                    ]
                ).get("ok")
            )
            time.sleep(0.15)
            before = d1_hold_client.status()
            self.assertTrue(before.get("hold_active"), before)
            pid = before.get("daemon_pid")
            released = d1_hold_client.publish(
                [{"seq": 23, "address": 1, "funcode": 5, "data": {"mode": 0}}]
            )
            self.assertTrue(released.get("ok"), released)
            count = int(released.get("heartbeat_count", 0))
            time.sleep(0.15)
            after = d1_hold_client.status()
            self.assertEqual(after.get("daemon_pid"), pid)
            self.assertFalse(after.get("hold_active"), after)
            self.assertEqual(int(after.get("heartbeat_count", 0)), count)

    def test_couple_without_pose_is_rejected(self) -> None:
        self._start()
        with patch.dict(os.environ, self.env, clear=False):
            result = d1_hold_client.publish(
                [{"seq": 30, "address": 1, "funcode": 5, "data": {"mode": 1}}]
            )
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result.get("reason"), "couple_requires_pose")

    def test_heartbeat_not_starved_by_delayed_publish_batch(self) -> None:
        """Regression: sleep under lock starved heartbeat → arm cedimento."""
        self._start()
        pose = self._pose(40)
        with patch.dict(os.environ, self.env, clear=False):
            self.assertTrue(
                d1_hold_client.publish(
                    [
                        {"seq": 41, "address": 1, "funcode": 6, "data": {"power": 1}},
                        {"seq": 42, "address": 1, "funcode": 5, "data": {"mode": 1}},
                        pose,
                    ]
                ).get("ok")
            )
            time.sleep(0.12)
            before = d1_hold_client.status()
            self.assertTrue(before.get("hold_active"), before)
            count0 = int(before.get("heartbeat_count", 0))
            batch = []
            for i in range(8):
                p = self._pose(50 + i)
                p["data"] = dict(p["data"])
                p["data"]["angle0"] = float(i)
                batch.append(p)
            # delay_ms between messages used to block the lock; heartbeat must rise.
            published = d1_hold_client.publish(batch, delay_ms=40)
            self.assertTrue(published.get("ok"), published)
            after = d1_hold_client.status()
            self.assertTrue(after.get("hold_active"), after)
            self.assertGreater(int(after.get("heartbeat_count", 0)), count0)
            self.assertLessEqual(float(after.get("heartbeat_age_ms") or 9999), 250.0)


if __name__ == "__main__":
    unittest.main()

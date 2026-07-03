#!/usr/bin/env python3
"""Gate non-motorio: daemon, feedback e deriva posa del D1."""
from __future__ import annotations

import argparse
import json
import time
import urllib.request


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=20) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://192.168.123.18:5056")
    parser.add_argument("--observe-s", type=float, default=5.0)
    parser.add_argument("--max-drift-deg", type=float, default=2.0)
    args = parser.parse_args()

    health0 = _get(args.base, "/api/health")
    pose0 = _get(args.base, "/api/joints/feedback").get("servo_deg")
    time.sleep(max(1.0, args.observe_s))
    health1 = _get(args.base, "/api/health")
    pose1 = _get(args.base, "/api/joints/feedback").get("servo_deg")

    daemon0 = health0.get("command_daemon") or {}
    daemon1 = health1.get("command_daemon") or {}
    if not isinstance(pose0, list) or not isinstance(pose1, list) or len(pose0) < 7 or len(pose1) < 7:
        raise SystemExit("ARM_STACK_FAIL no valid joint feedback")
    drift = [round(abs(float(pose1[i]) - float(pose0[i])), 3) for i in range(7)]
    checks = {
        "health_ok": health0.get("ok") is True and health1.get("ok") is True,
        "daemon_alive": daemon0.get("alive") is True and daemon1.get("alive") is True,
        "daemon_pid_stable": daemon0.get("pid") == daemon1.get("pid"),
        "startup_hold_ok": (health1.get("startup_arm_stabilization") or {}).get("ok") is True,
        "pose_drift_ok": max(drift) <= float(args.max_drift_deg),
        "external_hold": daemon0.get("external") is True and daemon1.get("external") is True,
        "hold_active": daemon0.get("hold_active") is True and daemon1.get("hold_active") is True,
        "heartbeat_advancing": int(daemon1.get("heartbeat_count") or 0) > int(daemon0.get("heartbeat_count") or 0),
    }
    result = {
        "ok": all(checks.values()),
        "checks": checks,
        "daemon_pid": daemon1.get("pid"),
        "pose_before": pose0,
        "pose_after": pose1,
        "drift_deg": drift,
        "max_drift_deg": max(drift),
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Poll ``/api/arm/scene_3d?fast=1`` on the dashboard; verify live servo joints (braccio fermo = piccola deriva)."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def fetch_fast(base: str, *, timeout: float) -> dict:
    qs = f"fast=1&_={int(time.time() * 1000)}"
    full = base.rstrip("/") + "/api/arm/scene_3d?" + qs
    req = urllib.request.Request(full, headers={"Cache-Control": "no-store"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify scene_3d fast returns live servo joints.")
    ap.add_argument(
        "--base",
        default="http://192.168.123.18:5050",
        help="Dashboard base URL",
    )
    ap.add_argument("--polls", type=int, default=5, help="Number of fast polls")
    ap.add_argument("--interval", type=float, default=0.22, help="Seconds between polls")
    ap.add_argument(
        "--max-joint-delta",
        type=float,
        default=4.0,
        help="Max |deg| change first vs last poll (braccio fermo / piegato)",
    )
    ap.add_argument(
        "--accept-demo-pose",
        action="store_true",
        help="Se manca feedback servo, verifica solo stabilità FK (pose demo) tra i poll.",
    )
    args = ap.parse_args()
    base = args.base.rstrip("/")

    samples: list[dict] = []
    try:
        for i in range(args.polls):
            if i:
                time.sleep(args.interval)
            samples.append(fetch_fast(base, timeout=25.0))
    except urllib.error.HTTPError as e:
        print("HTTP_FAIL", e.code, e.reason, file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        print("NET_FAIL", e, file=sys.stderr)
        return 2

    for i, j in enumerate(samples):
        if not j.get("ok"):
            print("JSON_NOT_OK", i, j.get("error"), file=sys.stderr)
            return 1
        sg = j.get("scene_graph") or {}
        loc = sg.get("d1_joint_locals_m")
        if not isinstance(loc, list) or len(loc) != 6:
            print("BAD_scene_graph.d1_joint_locals_m", i, file=sys.stderr)
            return 1

    servo_ok = all(bool(j.get("servo_feedback_ok")) for j in samples)
    if not servo_ok:
        if not args.accept_demo_pose:
            print(
                "NO_SERVO_FEEDBACK — niente lettura servo (DDS/seriale?). "
                "Rilancia con --accept-demo-pose per verificare solo stabilità FK senza hardware.",
                file=sys.stderr,
            )
            return 1
        s0 = json.dumps(samples[0]["scene_graph"].get("d1_joint_locals_m"), sort_keys=True)
        sn = json.dumps(samples[-1]["scene_graph"].get("d1_joint_locals_m"), sort_keys=True)
        if s0 != sn:
            print("DEMO_POSE_DRIFT scene_graph.d1_joint_locals_m first != last", file=sys.stderr)
            return 1
        tip = (samples[-1].get("scene_graph") or {}).get("tool_tip_xyz_m")
        print(
            "REALTIME_OK_DEMO_FK",
            args.polls,
            "polls (senza servo_feedback_ok; geometria stabile)",
            "tool_tip_xyz_m=",
            tip,
        )
        return 0

    for i, j in enumerate(samples):
        jd = j.get("joints_deg")
        if not isinstance(jd, list) or len(jd) < 6:
            print("BAD_joints_deg", i, jd, file=sys.stderr)
            return 1

    j0 = samples[0]["joints_deg"]
    jn = samples[-1]["joints_deg"]
    for k in range(6):
        d = abs(float(j0[k]) - float(jn[k]))
        if d > args.max_joint_delta:
            print(
                f"JOINT_DRIFT joint{k}: {j0[k]} -> {jn[k]} (|Δ|={d:.3f}° > {args.max_joint_delta})",
                file=sys.stderr,
            )
            return 1

    last = [round(float(x), 3) for x in jn[:6]]
    print(
        "REALTIME_OK_SERVO",
        args.polls,
        "polls",
        "joints_deg[0..5]=",
        last,
        "tool_tip_xyz_m=",
        (samples[-1].get("scene_graph") or {}).get("tool_tip_xyz_m"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Una sola chiamata Sport SDK — pensato per ``subprocess.run`` da Hermes/dashboard
(evita che un segfault CycloneDDS uccida il processo Flask).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from go2_accompany import dds_unitree_motion_ping, sport_accompany, sport_move, sport_simple_action


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True)
    ap.add_argument("--enable", default="1")
    ap.add_argument("--stand-up-first", dest="stand_first", default="0")
    ap.add_argument("--speed-level", dest="speed_level", default="")
    ap.add_argument("--vx", type=float, default=0.0)
    ap.add_argument("--vy", type=float, default=0.0)
    ap.add_argument("--vyaw", type=float, default=0.0)
    ap.add_argument("--duration", type=float, default=0.0)
    ap.add_argument("--action", default="")
    args = ap.parse_args()
    iface = (os.environ.get("GO2_DDS_INTERFACE") or "").strip() or None
    domain = int(os.environ.get("GO2_DDS_DOMAIN", "0"))
    mode = str(args.mode).strip().lower()

    if mode == "dds_ping":
        out = dds_unitree_motion_ping(
            project_root=ROOT,
            domain=domain,
            iface=iface,
        )
    elif mode == "move":
        out = sport_move(
            project_root=ROOT,
            domain=domain,
            iface=iface,
            vx=float(args.vx),
            vy=float(args.vy),
            vyaw=float(args.vyaw),
            duration_s=float(args.duration) if float(args.duration) > 0 else 0.9,
        )
    elif mode in {"stop", "hello", "stretch", "sit", "recovery", "balance"}:
        out = sport_simple_action(
            project_root=ROOT,
            domain=domain,
            iface=iface,
            action=mode if mode != "stop" else "stop",
        )
    elif mode == "simple" and str(args.action).strip():
        out = sport_simple_action(
            project_root=ROOT,
            domain=domain,
            iface=iface,
            action=str(args.action).strip(),
        )
    else:
        sl: int | None = None
        if str(args.speed_level).strip() != "":
            sl = int(args.speed_level)
        out = sport_accompany(
            project_root=ROOT,
            domain=domain,
            iface=iface,
            enable=str(args.enable).lower() in {"1", "true", "yes", "on"},
            mode=mode,
            stand_up_first=str(args.stand_first).lower() in {"1", "true", "yes", "on"},
            speed_level=sl,
        )
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()

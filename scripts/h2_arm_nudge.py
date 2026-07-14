#!/usr/bin/env python3
"""Micro-nudge joint 22 per verificare che rt/arm_sdk muova davvero il braccio."""
from __future__ import annotations

import argparse
import sys

from h2_common import (
    RIGHT_ARM_JOINTS,
    GentleArmConfig,
    init_dds,
    run_gentle_arm,
    wait_lowstate,
)


def main() -> int:
    p = argparse.ArgumentParser(description="H2: nudge spalla destra (joint 22) di pochi gradi")
    p.add_argument("--iface", default=None)
    p.add_argument("--delta", type=float, default=0.08, help="rad aggiuntivi su joint 22")
    p.add_argument("--rise", type=float, default=2.0)
    p.add_argument("--hold", type=float, default=2.0)
    p.add_argument("--lower", type=float, default=2.0)
    p.add_argument("--force", action="store_true", help="Ignora check FSM (solo debug)")
    args = p.parse_args()

    iface = init_dds(args.iface)
    print(f"[nudge] DDS iface={iface}")
    st = wait_lowstate()
    j = 22
    q0 = float(st.motor_state[j].q)
    target = {jj: float(st.motor_state[jj].q) for jj in RIGHT_ARM_JOINTS}
    target[j] = q0 + args.delta
    print(f"[nudge] joint {j}: q0={q0:.4f} target={target[j]:.4f} (delta={args.delta:+.4f})")

    cfg = GentleArmConfig(rise_s=args.rise, hold_s=args.hold, lower_s=args.lower, release_s=1.0)
    run_gentle_arm((j,), target, cfg=cfg, label="nudge-j22", force_fsm=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Gentle right arm forward + BrainCo Revo2 partial close (no Damp / no ReleaseMode)."""
from __future__ import annotations

import argparse
import sys

from h2_common import GentleArmConfig, init_dds, run_gentle_right_arm_front
from h2_hand_util import probe_right_hand, run_gentle_right_grip


def main() -> int:
    parser = argparse.ArgumentParser(description="H2 right arm front + hand grip test")
    parser.add_argument("--iface", default=None)
    parser.add_argument("--rise", type=float, default=8.0)
    parser.add_argument("--hold", type=float, default=2.0)
    parser.add_argument("--lower", type=float, default=8.0)
    parser.add_argument("--grip", type=float, default=0.55, help="0=open, 1=mid grip")
    parser.add_argument("--skip-arm", action="store_true")
    parser.add_argument("--skip-hand", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if not args.yes:
        print(
            "ATTENZIONE: braccio DESTRO avanti + presa mano destra.\n"
            "Robot in stand, area libera, operatore presente. Nessun Damp."
        )
        input("Enter per continuare...")

    init_dds(args.iface)

    if not args.skip_hand:
        topic, _ = probe_right_hand()
        if topic is None:
            print("[h2] mano destra non raggiungibile — prova solo braccio con --skip-hand", file=sys.stderr)
            if args.skip_arm:
                return 1

    if not args.skip_arm:
        cfg = GentleArmConfig(rise_s=args.rise, hold_s=args.hold, lower_s=args.lower)
        print("[h2] braccio destro avanti (lento)...")
        run_gentle_right_arm_front(cfg, on_phase=lambda p: print(f"  arm: {p}"))

    if not args.skip_hand:
        print("[h2] presa mano destra (lenta)...")
        run_gentle_right_grip(close_fraction=args.grip)

    print("[h2] right arm+hand test done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

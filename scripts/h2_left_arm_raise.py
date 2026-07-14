#!/usr/bin/env python3
"""Step 2: gentle left arm raise only (slow ramp, no audio)."""
from __future__ import annotations

import argparse
import sys

from h2_common import GentleArmConfig, init_dds, run_gentle_left_arm


def main() -> int:
    parser = argparse.ArgumentParser(description="H2 gentle left arm")
    parser.add_argument("--iface", default=None)
    parser.add_argument("--rise", type=float, default=8.0, help="seconds to reach pose")
    parser.add_argument("--hold", type=float, default=2.0)
    parser.add_argument("--lower", type=float, default=8.0)
    parser.add_argument("--yes", action="store_true", help="skip Enter confirmation")
    args = parser.parse_args()

    if not args.yes:
        print(
            "ATTENZIONE: movimento LENTO braccio sinistro via rt/arm_sdk.\n"
            "Il robot deve essere in piedi, area libera, operatore presente.\n"
            "NON viene rilasciata la modalità locomozione (come esempio ufficiale G1)."
        )
        input("Premi Enter per continuare...")

    iface = init_dds(args.iface)
    print(f"[h2] DDS on {iface}")

    cfg = GentleArmConfig(rise_s=args.rise, hold_s=args.hold, lower_s=args.lower)

    def _log(phase: str) -> None:
        print(f"[h2] phase: {phase}")

    run_gentle_left_arm(cfg, on_phase=_log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

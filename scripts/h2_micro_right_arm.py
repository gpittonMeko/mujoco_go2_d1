#!/usr/bin/env python3
"""Micromovimento lento braccio destro H2 (test sicurezza prima del demo)."""
from __future__ import annotations

import argparse

from h2_common import GentleArmConfig, init_dds, run_micro_right_arm


def main() -> int:
    parser = argparse.ArgumentParser(description="H2 micro right arm test")
    parser.add_argument("--iface", default=None)
    parser.add_argument("--rise", type=float, default=12.0)
    parser.add_argument("--hold", type=float, default=2.0)
    parser.add_argument("--lower", type=float, default=12.0)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if not args.yes:
        print(
            "Micromovimento DESTRO (~10 cm): molto lento, poi torna indietro.\n"
            "Robot in stand, area libera. STOP: h2_smoke_remote.py arm-stop"
        )
        input("Enter per continuare...")

    init_dds(args.iface)
    cfg = GentleArmConfig(rise_s=args.rise, hold_s=args.hold, lower_s=args.lower)
    run_micro_right_arm(cfg=cfg, on_phase=lambda p: print(f"  arm: {p}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Step 0: verify DDS lowstate on H2."""
from __future__ import annotations

import argparse
import sys

from h2_common import init_dds, wait_lowstate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", default=None)
    args = parser.parse_args()

    iface = init_dds(args.iface)
    print(f"[h2] DDS iface={iface}")
    msg = wait_lowstate()
    n = len(msg.motor_state)
    print(f"[h2] OK motors={n}")
    if n > 0:
        for j in (15, 16, 17, 18):
            print(f"  joint {j} q={msg.motor_state[j].q:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

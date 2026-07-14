#!/usr/bin/env python3
"""Read-only: probe BrainCo Revo2 right hand state on H2."""
from __future__ import annotations

import argparse

from h2_common import init_dds
from h2_hand_util import probe_right_hand


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", default=None)
    args = parser.parse_args()
    init_dds(args.iface)
    topic, msg = probe_right_hand()
    if topic is None:
        print("[hand] FAIL: nessun topic mano destra risponde", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

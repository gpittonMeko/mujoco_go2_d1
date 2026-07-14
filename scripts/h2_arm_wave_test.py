#!/usr/bin/env python3
"""Test high-level arm task (WaveHand) — verifica se il braccio risponde via Loco API."""
from __future__ import annotations

import argparse
import sys
import time

from h2_common import ensure_sdk_path, get_h2_fsm_id, init_dds


def main() -> int:
    p = argparse.ArgumentParser(description="H2 WaveHand via SetTaskId (API 7106)")
    p.add_argument("--iface", default=None)
    args = p.parse_args()
    init_dds(args.iface)
    ensure_sdk_path()
    from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

    code, fsm = get_h2_fsm_id()
    print(f"[wave] FSM={fsm} (code={code})")
    loco = LocoClient()
    loco.SetTimeout(5.0)
    loco.Init()
    print("[wave] WaveHand(0) — saluto braccio...")
    c0 = loco.WaveHand(False)
    print(f"[wave] code={c0}")
    time.sleep(4.0)
    print("[wave] StopMove + WaveHand off")
    loco.StopMove()
    return 0


if __name__ == "__main__":
    sys.exit(main())

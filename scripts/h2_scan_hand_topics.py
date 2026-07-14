#!/usr/bin/env python3
"""Scan BrainCo / Inspire / Dex hand DDS topics on H2 (read-only)."""
from __future__ import annotations

import argparse
import time

from h2_common import init_dds

TOPICS = (
    ("rt/brainco/right/state", "brainco"),
    ("rt/brainco/left/state", "brainco"),
    ("rt/inspire/state", "inspire"),
    ("rt/dex3/right/state", "dex3"),
    ("rt/dex1/right/state", "dex1"),
)


def try_topic(topic: str, kind: str, timeout_s: float) -> bool:
    if kind == "brainco" or kind == "dex1":
        from unitree_sdk2py.core.channel import ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorStates_

        cls = MotorStates_
    elif kind == "inspire":
        from unitree_sdk2py.core.channel import ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorStates_

        cls = MotorStates_
    else:
        from unitree_sdk2py.core.channel import ChannelSubscriber
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import HandState_

        cls = HandState_

    sub = ChannelSubscriber(topic, cls)
    state = {"msg": None}

    def _handler(msg):
        if msg is not None:
            state["msg"] = msg

    sub.Init(_handler, 10)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if state["msg"] is not None:
            n = len(getattr(state["msg"], "states", None) or getattr(state["msg"], "motor_state", None) or [])
            print(f"  OK {topic} ({kind}) motors={n}")
            return True
        time.sleep(0.05)
    print(f"  -- {topic}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", default=None)
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()
    init_dds(args.iface)
    print("[hand-scan] H2 hand topics...")
    any_ok = any(try_topic(t, k, args.timeout) for t, k in TOPICS)
    return 0 if any_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

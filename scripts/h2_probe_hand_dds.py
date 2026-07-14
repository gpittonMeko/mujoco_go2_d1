#!/usr/bin/env python3
"""Probe BrainCo hands via DDS dalla Jetson — SOLO eth10/eth11, niente SSH .162."""
from __future__ import annotations

import argparse
import time

TOPICS = (
    "rt/brainco/right/state",
    "rt/brainco/left/state",
    "rt/brainco/right/cmd",
    "rt/brainco/left/cmd",
)


def listen(iface: str, topic: str, timeout_s: float) -> bool:
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorStates_

    ChannelFactoryInitialize(0, iface)
    got = {"msg": None, "n": 0}

    def _handler(msg: MotorStates_):
        got["n"] += 1
        if msg is not None and len(msg.states) >= 6:
            got["msg"] = msg

    sub = ChannelSubscriber(topic, MotorStates_)
    sub.Init(_handler, 10)
    t0 = time.time()
    while time.time() - t0 < timeout_s and got["msg"] is None:
        time.sleep(0.05)

    if got["msg"] is not None:
        qs = [f"{got['msg'].states[i].q:.3f}" for i in range(6)]
        print(f"  OK  {topic} iface={iface} samples={got['n']} q={qs}")
        return True
    print(f"  --  {topic} iface={iface} (no data in {timeout_s:.0f}s)")
    return False


def main() -> int:
    p = argparse.ArgumentParser(description="DDS hand probe from Jetson (no SSH)")
    p.add_argument("--iface", default=None, help="eth10 (default) or try --all-ifaces")
    p.add_argument("--all-ifaces", action="store_true")
    p.add_argument("--timeout", type=float, default=8.0)
    args = p.parse_args()

    ifaces = ["eth10", "eth11"] if args.all_ifaces or not args.iface else [args.iface]
    print(f"[hand-dds] probe from Jetson, ifaces={ifaces}, timeout={args.timeout}s")
    any_ok = False
    for iface in ifaces:
        print(f"[hand-dds] --- {iface} ---")
        for topic in TOPICS:
            if "state" in topic and listen(iface, topic, args.timeout):
                any_ok = True
    if any_ok:
        print("[hand-dds] Mani visibili via DDS dalla Jetson.")
        return 0
    print(
        "[hand-dds] NESSUN publisher rt/brainco/* su eth10.\n"
        "  Il bridge seriale→DDS deve girare sul PC interno robot (PC2).\n"
        "  La Thor .163 è solo client DDS — come lowstate/arm_sdk.",
        file=__import__("sys").stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

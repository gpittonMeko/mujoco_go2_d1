#!/usr/bin/env python3
"""Diagnostica braccio H2: FSM, arm task, arm_sdk — read-only + opzionale --try-wave."""
from __future__ import annotations

import argparse
import json
import sys
import time

from h2_common import (
    LOCO_API_GET_ARM_SDK,
    LOCO_API_SET_ARM_SDK,
    ensure_sdk_path,
    fsm_label,
    get_h2_arm_sdk_status,
    get_h2_fsm_id,
    init_dds,
    wait_lowstate,
)


def _loco():
    ensure_sdk_path()
    from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

    loco = LocoClient()
    loco.SetTimeout(5.0)
    loco.Init()
    loco._RegistApi(LOCO_API_GET_ARM_SDK, 0)
    loco._RegistApi(LOCO_API_SET_ARM_SDK, 0)
    loco._RegistApi(7008, 0)  # GET_AVAILABLE_FSM_IDS
    return loco


def _call(loco, api: int, payload: str = "{}") -> tuple[int, str | None]:
    code, data = loco._Call(api, payload)
    return code, data


def _print_json(label: str, code: int, raw: str | None) -> None:
    print(f"[diag] {label} code={code}", end="")
    if not raw:
        print(" data=<empty>")
        return
    try:
        print(f" data={json.loads(raw)}")
    except Exception:
        print(f" data={raw!r}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--iface", default=None)
    p.add_argument("--try-wave", action="store_true", help="Prova SetTaskId(0) e logga risposta")
    p.add_argument("--release-hold", action="store_true", help="Invia SetTaskId(99) per rilasciare hold")
    args = p.parse_args()

    init_dds(args.iface)
    loco = _loco()

    code, fsm = get_h2_fsm_id()
    print(f"[diag] FSM={fsm_label(fsm)} (read code={code})")

    _print_json("GetFsmMode", *_call(loco, 7002))
    _print_json("GetBalanceMode", *_call(loco, 7003))
    _print_json("GetAvailableFsmIds", *_call(loco, 7008))

    code, enabled = get_h2_arm_sdk_status()
    print(f"[diag] GetArmSdkStatus enabled={enabled} code={code}")

    st = wait_lowstate()
    print(f"[diag] lowstate mode_machine={int(st.mode_machine)} motors={len(st.motor_state)}")
    for j in (22, 25, 31):
        print(f"[diag] motor[{j}] q={st.motor_state[j].q:.4f}")

    if args.release_hold:
        print("[diag] SetTaskId(99) release hold...")
        _print_json("SetTaskId(99)", *_call(loco, 7106, json.dumps({"data": 99})))
        time.sleep(1.0)

    if args.try_wave:
        print("[diag] SetTaskId(0) wave...")
        c0, d0 = _call(loco, 7106, json.dumps({"data": 0}))
        _print_json("SetTaskId(0)", c0, d0)
        if c0 != 0:
            print(
                f"[diag] SetTaskId fallito — serve FSM={fsm_label(4)} (FixStand), "
                f"non {fsm_label(fsm)}.",
                file=sys.stderr,
            )
        elif fsm != 4:
            print(
                f"[diag] SetTaskId code=0 ma FSM={fsm_label(fsm)}: API accettata, "
                "braccio spesso NON si muove finché non sei in FixStand (4).",
                file=sys.stderr,
            )
        t0 = time.time()
        while time.time() - t0 < 4.0:
            st = wait_lowstate(timeout_s=2.0)
            print(f"[diag] live j22={st.motor_state[22].q:.4f} j25={st.motor_state[25].q:.4f}")
            time.sleep(1.0)

    print(
        "[diag] Doc/SDK: joint 22=spalla destra, peso arm_sdk=31. "
        "Mani BrainCo: DDS rt/brainco/* su eth10 (Thor = solo client DDS)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

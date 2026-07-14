#!/usr/bin/env python3
"""Read-only: verifica arm_sdk H2 (Loco API + q spalla destra)."""
from __future__ import annotations

import argparse

from h2_common import ensure_sdk_path, get_h2_arm_sdk_status, init_dds, wait_lowstate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", default=None)
    args = parser.parse_args()
    init_dds(args.iface)
    ensure_sdk_path()
    from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

    loco = LocoClient()
    loco.SetTimeout(5.0)
    loco.Init()
    code, enabled = get_h2_arm_sdk_status()
    print(f"[arm] GetArmSdkStatus code={code} enabled={enabled}")
    fsm_code, fsm_data = loco._Call(7001, "{}")
    if fsm_code == 0 and fsm_data:
        import json
        try:
            print(f"[arm] FSM id={json.loads(fsm_data).get('data')}")
        except Exception:
            print(f"[arm] FSM raw={fsm_data!r}")
    st = wait_lowstate()
    print(f"[arm] lowstate mode_machine={int(st.mode_machine)}")
    for j in (22, 23, 25, 29, 31):
        if j < len(st.motor_state):
            print(f"[arm] motor[{j}] q={st.motor_state[j].q:.4f}")
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

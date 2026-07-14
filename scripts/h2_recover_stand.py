#!/usr/bin/env python3
"""Recover H2 locomotion mode — SENZA Damp (pericoloso sul protection frame)."""
from __future__ import annotations

import argparse
import sys
import time

from h2_common import ensure_sdk_path, fsm_label, get_h2_fsm_id, init_dds

LOCOMOTION_MODE = "ai"

# H2 official FSM — Damp/StandUp NON usati di default (manda giù il robot sul frame)
FSM_DAMP = 1
FSM_STAND_UP = 4
FSM_READY = 601


def _check_mode() -> tuple[int, dict | None]:
    ensure_sdk_path()
    from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient

    msc = MotionSwitcherClient()
    msc.SetTimeout(5.0)
    msc.Init()
    return msc.CheckMode()


def _print_mode(label: str) -> tuple[int, dict | None]:
    code, info = _check_mode()
    if code != 0 or not info:
        print(f"[recover] {label}: CheckMode code={code} info={info!r}")
        if code == 0 and not info:
            print("[recover] Nessuna modalità locomozione attiva (probabile debug mode).")
        return code, info
    name = info.get("name") if isinstance(info, dict) else str(info)
    print(f"[recover] {label}: mode={name!r} info={info}")
    return code, info


def _set_fsm(loco, fsm_id: int, label: str) -> int | None:
    code = loco.SetFsmId(fsm_id)
    print(f"[recover] {label} (FSM {fsm_id}) code={code}")
    if code not in (0, None):
        print(f"[recover] {label} fallito.", file=sys.stderr)
    return code


def cmd_check(iface: str | None) -> int:
    init_dds(iface)
    print("[recover] --check (solo lettura, nessun comando al robot)")
    _print_mode("attuale")
    code, fsm = get_h2_fsm_id()
    print(f"[recover] FSM attuale: {fsm_label(fsm)} (code={code})")
    return 0


def cmd_select_ai(iface: str | None) -> int:
    """Solo SelectMode(ai) — nessun cambio FSM. Sicuro per demo braccio su frame."""
    init_dds(iface)
    ensure_sdk_path()
    from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient

    print("[recover] select-ai: solo SelectMode(ai), NESSUN Damp/StandUp")
    _print_mode("prima")
    code, fsm = get_h2_fsm_id()
    print(f"[recover] FSM prima: {fsm_label(fsm)}")

    msc = MotionSwitcherClient()
    msc.SetTimeout(5.0)
    msc.Init()
    code_sel, _ = msc.SelectMode(LOCOMOTION_MODE)
    print(f"[recover] SelectMode({LOCOMOTION_MODE!r}) code={code_sel}")
    time.sleep(1.0)
    _print_mode("dopo")
    code, fsm = get_h2_fsm_id()
    print(f"[recover] FSM dopo: {fsm_label(fsm)}")
    print("[recover] OK — usa arm-nudge/micro-arm senza recover-stand.")
    return 0 if code_sel == 0 else 1


def cmd_recover(iface: str | None, confirmed: bool, with_damp: bool) -> int:
    if not confirmed:
        print(
            "ERRORE: recovery richiede --confirm e protection frame montato.\n"
            "Per la demo braccio usa invece: --select-ai (nessun Damp).\n"
            "Rilancia: python3 scripts/h2_recover_stand.py --recover --confirm",
            file=sys.stderr,
        )
        return 2

    if with_damp:
        print(
            "ATTENZIONE: --with-damp usa Damp(1) + StandUp(4) — PUÒ MANDARE GIÙ IL ROBOT.\n"
            "Usa solo se sai cosa stai facendo."
        )
    else:
        print(
            "ATTENZIONE: recovery senza Damp — solo SelectMode(ai).\n"
            "Per sequenza Damp/StandUp (pericolosa): aggiungi --with-damp."
        )

    if sys.stdin.isatty():
        input("Premi Enter per avviare...")
    else:
        print("[recover] avvio non-interattivo (--confirm da smoke remote)")

    init_dds(iface)
    ensure_sdk_path()
    from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient
    from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

    print("[recover] 1/3 CheckMode (prima)")
    _print_mode("prima")
    code, fsm = get_h2_fsm_id()
    print(f"[recover] FSM prima: {fsm_label(fsm)}")

    print(f"[recover] 2/3 SelectMode({LOCOMOTION_MODE!r})")
    msc = MotionSwitcherClient()
    msc.SetTimeout(5.0)
    msc.Init()
    code_sel, _ = msc.SelectMode(LOCOMOTION_MODE)
    print(f"[recover] SelectMode code={code_sel}")
    if code_sel != 0:
        return 1
    time.sleep(1.5)

    if not with_damp:
        print("[recover] 3/3 fatto (no Damp). Verifica FSM dal telecomando.")
        _print_mode("finale")
        code, fsm = get_h2_fsm_id()
        print(f"[recover] FSM finale: {fsm_label(fsm)}")
        return 0

    loco = LocoClient()
    loco.SetTimeout(10.0)
    loco.Init()

    print("[recover] 3/5 Loco: Damp -> Ready -> StandUp (PERICOLOSO)")
    if _set_fsm(loco, FSM_DAMP, "Damp") not in (0, None):
        return 1
    time.sleep(0.5)
    if _set_fsm(loco, FSM_READY, "Ready") not in (0, None):
        return 1
    time.sleep(2.0)
    if _set_fsm(loco, FSM_STAND_UP, "StandUp") not in (0, None):
        return 1
    code_hs = loco.HighStand()
    print(f"[recover] HighStand code={code_hs}")
    time.sleep(6.0)

    print("[recover] CheckMode (finale)")
    _print_mode("finale")
    code, fsm = get_h2_fsm_id()
    print(f"[recover] FSM finale: {fsm_label(fsm)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="H2 recovery: select ai mode (no Damp by default)")
    parser.add_argument("--iface", default=None, help="DDS interface (default eth10)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="Solo diagnostica (default)")
    group.add_argument("--select-ai", action="store_true", help="Solo SelectMode(ai), sicuro per demo braccio")
    group.add_argument("--recover", action="store_true", help="SelectMode(ai); con --with-damp anche Damp/Stand")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Obbligatorio con --recover: protection frame montato",
    )
    parser.add_argument(
        "--with-damp",
        action="store_true",
        help="Con --recover: sequenza Damp->Ready->StandUp (PERICOLOSO sul frame)",
    )
    args = parser.parse_args()

    if args.select_ai:
        return cmd_select_ai(args.iface)
    if args.recover:
        return cmd_recover(args.iface, args.confirm, args.with_damp)
    return cmd_check(args.iface)


if __name__ == "__main__":
    raise SystemExit(main())

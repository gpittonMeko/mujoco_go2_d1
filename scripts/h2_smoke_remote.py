#!/usr/bin/env python3
"""Run H2 demo smoke tests on Jetson Thor via SSH (one step at a time)."""
from __future__ import annotations

import argparse
import os
import sys
import time

import paramiko

HOST = os.environ.get("H2_JETSON_HOST", "192.168.123.163")
USER = os.environ.get("H2_JETSON_USER", "unitree")
PW = os.environ.get("H2_JETSON_PASSWORD", "123")
REMOTE = "/home/unitree/h2_demo"
IFACE = os.environ.get("H2_DDS_IFACE", "eth10")

ENV_PREFIX = (
    "export CYCLONEDDS_HOME=/usr/local "
    "LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH "
    "PATH=$HOME/.local/bin:$PATH "
    f"H2_DDS_IFACE={IFACE}; "
)

SAFE_STEPS: dict[str, tuple[str, int]] = {
    "ping": ("echo OK Jetson $(hostname) && ip -4 addr show eth10 | grep inet", 30),
    "install-sdk": (f"bash {REMOTE}/install_sdk.sh", 300),
    "recover-check": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_recover_stand.py --check --iface {IFACE}",
        45,
    ),
    "verify-sdk": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_verify_sdk.py --iface {IFACE}",
        45,
    ),
    "lowstate": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_probe_lowstate.py --iface {IFACE}",
        45,
    ),
    "hand-probe": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_probe_hand.py --iface {IFACE}",
        45,
    ),
    "hand-scan": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_scan_hand_topics.py --iface {IFACE}",
        60,
    ),
    "hand-dds": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_probe_hand_dds.py --iface {IFACE} --timeout 12",
        60,
    ),
    "tts": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_test_tts.py --iface {IFACE}",
        45,
    ),
    "wav": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_play_wav.py --iface {IFACE}",
        120,
    ),
    "demo-dry-audio": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_demo_casoria.py --iface {IFACE} --yes --dry-arm",
        90,
    ),
    "arm-stop": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_arm_emergency_stop.py --iface {IFACE}",
        20,
    ),
    "arm-sdk-check": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_verify_arm_sdk.py --iface {IFACE}",
        30,
    ),
    "arm-diag": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_arm_diag.py --iface {IFACE}",
        30,
    ),
}

RECOVERY_STEPS: dict[str, tuple[str, int]] = {
    "recover-select-ai": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_recover_stand.py --select-ai --iface {IFACE}",
        60,
    ),
    "recover-stand": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_recover_stand.py --recover --confirm --iface {IFACE}",
        120,
    ),
}

ARM_STEPS: dict[str, tuple[str, int]] = {
    "arm": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_left_arm_raise.py --iface {IFACE} --yes",
        120,
    ),
    "right-arm": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_right_arm_hand.py --iface {IFACE} --yes",
        180,
    ),
    "right-arm-only": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_right_arm_hand.py --iface {IFACE} --yes --skip-hand",
        120,
    ),
    "hand-grip": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_right_arm_hand.py --iface {IFACE} --yes --skip-arm",
        90,
    ),
    "micro-arm": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_micro_right_arm.py --iface {IFACE} --yes",
        120,
    ),
    "arm-nudge": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_arm_nudge.py --iface {IFACE}",
        60,
    ),
    "arm-wave": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_arm_wave_test.py --iface {IFACE}",
        30,
    ),
    "arm-diag-wave": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_arm_diag.py --iface {IFACE} --try-wave",
        45,
    ),
    "demo-dry-arm": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_demo_casoria.py --iface {IFACE} --yes --dry-audio --rise 12 --hold 2 --lower 12",
        150,
    ),
    "demo": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_demo_casoria.py --iface {IFACE} --yes",
        360,
    ),
    "demo-slow": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_demo_casoria.py --iface {IFACE} --yes --rise 12 --lower 12",
        420,
    ),
    "demo-legacy": (
        f"cd {REMOTE} && {ENV_PREFIX} python3 scripts/h2_demo_pellegrino.py --iface {IFACE} --yes",
        180,
    ),
}


def run_remote(cmd: str, timeout: int) -> int:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"SSH {USER}@{HOST} ...")
    c.connect(HOST, username=USER, password=PW, timeout=20, allow_agent=False, look_for_keys=False)
    print(f"$ {cmd}\n")
    t0 = time.time()
    _, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    c.close()
    if out:
        safe = out.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8", errors="replace"
        )
        print(safe, end="" if safe.endswith("\n") else "\n")
    if err.strip():
        print(err, file=sys.stderr)
    print(f"\n--- exit {code} ({time.time() - t0:.1f}s) ---")
    return code


def main() -> int:
    all_steps = {**SAFE_STEPS, **RECOVERY_STEPS, **ARM_STEPS}
    parser = argparse.ArgumentParser(description="H2 smoke tests on Jetson (one at a time)")
    parser.add_argument(
        "step",
        choices=list(all_steps.keys()) + ["list"],
        nargs="?",
        default="list",
        help="Smoke step to run",
    )
    parser.add_argument(
        "--confirm-arm",
        action="store_true",
        help="Obbligatorio per step arm/demo: conferma area libera e robot in piedi",
    )
    parser.add_argument(
        "--confirm-recovery",
        action="store_true",
        help="Obbligatorio per recover-stand: protection frame montato",
    )
    args = parser.parse_args()
    if args.step == "list":
        print("Smoke steps (run in order):")
        for i, name in enumerate(all_steps, 1):
            if name in ARM_STEPS:
                tag = " [ARM — serve --confirm-arm]"
            elif name in RECOVERY_STEPS:
                tag = " [RECOVERY — serve --confirm-recovery]"
            else:
                tag = ""
            print(f"  {i}. {name}{tag}")
        return 0

    if args.step in ARM_STEPS and not args.confirm_arm:
        print(
            f"RIFIUTATO: step '{args.step}' muove il braccio.\n"
            "Rilancia con --confirm-arm solo se robot in piedi, area libera, operatore presente.",
            file=sys.stderr,
        )
        return 2

    if args.step in RECOVERY_STEPS and not args.confirm_recovery:
        print(
            f"RIFIUTATO: step '{args.step}' comanda stand sul robot.\n"
            "Rilancia con --confirm-recovery solo se protection frame montato e operatore presente.",
            file=sys.stderr,
        )
        return 2

    cmd, timeout = all_steps[args.step]
    print(f"=== smoke: {args.step} ===")
    return run_remote(cmd, timeout)


if __name__ == "__main__":
    raise SystemExit(main())

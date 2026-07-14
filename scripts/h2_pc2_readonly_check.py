#!/usr/bin/env python3
"""Read-only probe of H2 PC2 (.162) — no hand motion, no service start."""
from __future__ import annotations

import paramiko
import sys

HOST = "192.168.123.162"
USER = "unitree"
PW = "Unitree#24226"

REMOTE = r"""
set +e
echo "=== identity ==="
hostname
whoami
uname -m
ip -4 addr show | grep -E 'inet 192\.168'

echo "=== serial devices (hands?) ==="
ls -la /dev/ttyUSB* /dev/ttyHAND* /dev/ttyUN* 2>&1 | head -20

echo "=== brainco process ==="
pgrep -a brainco || echo "(no brainco process)"

echo "=== brainco systemd ==="
systemctl is-active brainco_hand.service 2>/dev/null || echo "brainco_hand.service: not active/unknown"
systemctl is-enabled brainco_hand.service 2>/dev/null || true
systemctl list-unit-files 2>/dev/null | grep -i brain || echo "(no brain* unit files)"

echo "=== brainco install dirs ==="
ls -d ~/brainco_hand_service ~/brainco_hand_service/build 2>/dev/null || echo "(no ~/brainco_hand_service)"
ls ~/brainco_hand_service/bin 2>/dev/null | head -10 || true
ls ~/brainco_hand_service/build 2>/dev/null | head -10 || true

echo "=== dmesg usb tail (read-only) ==="
dmesg 2>/dev/null | grep -iE 'ttyUSB|usb|brain|serial' | tail -15 || echo "(dmesg unavailable)"

echo "=== DONE read-only ==="
"""


def main() -> int:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"SSH read-only {USER}@{HOST} ...")
    try:
        c.connect(HOST, username=USER, password=PW, timeout=15, allow_agent=False, look_for_keys=False)
    except Exception as e:
        print(f"CONNECT FAIL: {e}", file=sys.stderr)
        return 1

    stdin, stdout, stderr = c.exec_command("bash -s", timeout=45)
    stdin.write(REMOTE)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    safe = out.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
        sys.stdout.encoding or "utf-8", errors="replace"
    )
    print(safe)
    if err.strip():
        print("STDERR:", err, file=sys.stderr)
    c.close()
    print(f"exit {code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

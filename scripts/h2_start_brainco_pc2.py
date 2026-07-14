#!/usr/bin/env python3
"""Start brainco_hand_server on H2 PC2 (.162) and verify DDS from Thor."""
from __future__ import annotations

import argparse
import sys
import time

import paramiko

PC2 = "192.168.123.162"
THOR = "192.168.123.163"
USER = "unitree"
PC2_PW = "Unitree#24226"
THOR_PW = "123"
BIN = "/home/unitree/brainco_hand_service/bin/brainco_hand_server"
IFACE = "eth0"


def ssh_run(host: str, password: str, script: str, timeout: int = 60) -> tuple[int, str, str]:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=USER, password=password, timeout=20, allow_agent=False, look_for_keys=False)
    stdin, stdout, stderr = c.exec_command("bash -s", timeout=timeout)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    c.close()
    return code, out, err


def print_safe(text: str) -> None:
    enc = sys.stdout.encoding or "utf-8"
    print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def start_server() -> bool:
    script = f"""
set +e
PW='{PC2_PW}'
BIN='{BIN}'
IFACE='{IFACE}'

echo "=== stop old brainco ==="
echo "$PW" | sudo -S pkill -f brainco_hand_server 2>/dev/null || true
sleep 1

echo "=== start brainco_hand_server ==="
if [ ! -x "$BIN" ]; then echo "MISSING $BIN"; exit 2; fi
echo "$PW" | sudo -S nohup "$BIN" -n "$IFACE" > /tmp/brainco_hand_server.log 2>&1 &
sleep 3

echo "=== process ==="
pgrep -a brainco_hand || echo "NO_PROCESS"

echo "=== log tail ==="
tail -30 /tmp/brainco_hand_server.log 2>/dev/null || true

if pgrep -f brainco_hand_server >/dev/null; then
  echo "START_OK"
  exit 0
else
  echo "START_FAIL"
  exit 1
fi
"""
    code, out, err = ssh_run(PC2, PC2_PW, script, timeout=45)
    print_safe(out)
    if err.strip():
        print_safe("STDERR: " + err)
    return code == 0 and "START_OK" in out


def verify_hand_dds() -> bool:
    script = """
export CYCLONEDDS_HOME=/usr/local
export LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH
export PATH=$HOME/.local/bin:$PATH
export H2_DDS_IFACE=eth10
cd ~/h2_demo
python3 scripts/h2_probe_hand_dds.py --iface eth10 --timeout 12
"""
    code, out, err = ssh_run(THOR, THOR_PW, script, timeout=90)
    print_safe(out)
    if err.strip():
        print_safe("STDERR: " + err)
    return code == 0 and "OK" in out


def run_hand_grip() -> int:
    script = """
export CYCLONEDDS_HOME=/usr/local
export LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH
export PATH=$HOME/.local/bin:$PATH
export H2_DDS_IFACE=eth10
cd ~/h2_demo
python3 -c "
import sys
sys.path.insert(0, 'scripts')
from h2_common import init_dds
from h2_hand_util import run_gentle_left_grip
init_dds('eth10')
run_gentle_left_grip(close_fraction=0.45)
"
"""
    code, out, err = ssh_run(THOR, THOR_PW, script, timeout=120)
    print_safe(out)
    if err.strip():
        print_safe("STDERR: " + err)
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grip", action="store_true", help="Run gentle left-hand grip after DDS OK")
    args = parser.parse_args()

    print("=== 1) Start brainco_hand_server on PC2 .162 ===")
    if not start_server():
        print("FAILED to start brainco_hand_server", file=sys.stderr)
        return 1

    print("\n=== 2) Verify rt/brainco/* from Thor .163 ===")
    time.sleep(2)
    if not verify_hand_dds():
        print("hand-dds still empty — check /tmp/brainco_hand_server.log on .162", file=sys.stderr)
        return 1

    if not args.grip:
        print("\nDDS OK. Rerun with --grip to test left finger motion.")
        return 0

    print("\n=== 3) Gentle left-hand grip ===")
    return run_hand_grip()


if __name__ == "__main__":
    raise SystemExit(main())

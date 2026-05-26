#!/usr/bin/env python3
"""Dalla Jetson (SSH come deploy): mostra GO2_ANYGRASP_WORKER_URL e prova TCP 8765 su 192.168.123.x.

Uso (PC in LAN verso NX):
  python scripts/nx_scan_grasp_worker_port.py [porta]
"""
from __future__ import annotations

import os
import sys

import paramiko


def nx_host() -> str:
    return (os.environ.get("GO2_NX_HOST") or "192.168.123.18").strip() or "192.168.123.18"


def nx_user() -> str:
    return (os.environ.get("GO2_NX_USER") or "unitree").strip() or "unitree"


def nx_password() -> str:
    return os.environ.get("GO2_NX_PASSWORD") or "123"


def main() -> int:
    port = (sys.argv[1] if len(sys.argv) > 1 else "8765").strip()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"[nx-scan] SSH {nx_user()}@{nx_host()} …", flush=True)
    ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=30)
    script = f"""set +e
echo '=== GO2_ANYGRASP_WORKER_URL (nx_dashboard_env.sh) ==='
grep GO2_ANYGRASP_WORKER_URL /home/unitree/go2_visual_dashboard/scripts/nx_dashboard_env.sh 2>/dev/null || true
echo "=== scan TCP {port} su 192.168.123.1–60 + alcuni host ==="
for n in $(seq 1 60) 100 120 161 200 220; do
  ip=192.168.123.$n
  if timeout 0.28 bash -c "echo >/dev/tcp/$ip/{port}" 2>/dev/null; then
    echo "OPEN:$ip"
  fi
done
"""
    stdin, stdout, stderr = ssh.exec_command("bash -s")
    stdin.write(script)
    stdin.channel.shutdown_write()
    stdout.channel.recv_exit_status()
    print(stdout.read().decode(errors="replace").rstrip())
    se = stderr.read().decode(errors="replace").strip()
    if se:
        print("stderr:", se[:800], file=sys.stderr)
    ssh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

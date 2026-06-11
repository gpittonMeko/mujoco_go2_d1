#!/usr/bin/env python3
"""Aggiorna sulla Jetson ``GO2_ANYGRASP_WORKER_URL`` in ``nx_dashboard_env.sh`` e riavvia la dashboard.

Uso (PC in LAN verso NX):
  python scripts/nx_set_grasp_worker_url.py http://192.168.123.3:8765

Env: GO2_NX_HOST, GO2_NX_USER, GO2_NX_PASSWORD (come deploy).
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
    if len(sys.argv) < 2:
        print("Usage: nx_set_grasp_worker_url.py http://<host>:8765", file=sys.stderr)
        return 2
    url = sys.argv[1].strip().rstrip("/")
    if "://" not in url or not url.startswith("http"):
        print("URL deve essere tipo http://192.168.123.3:8765", file=sys.stderr)
        return 2
    esc = url.replace("|", "\\|").replace("&", "\\&")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"[nx-set-worker] SSH {nx_user()}@{nx_host()} -> {url}")
    ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=30)
    path = "/home/unitree/go2_visual_dashboard/scripts/nx_dashboard_env.sh"
    # | come delimitatore sed: URL contiene /
    cmd = (
        f"sed -i 's|^export GO2_ANYGRASP_WORKER_URL=.*|export GO2_ANYGRASP_WORKER_URL={esc}|' {path} "
        f"&& grep GO2_ANYGRASP_WORKER_URL {path}"
    )
    _, stdout, stderr = ssh.exec_command(cmd)
    stdout.channel.recv_exit_status()
    print(stdout.read().decode(errors="replace").rstrip())
    se = stderr.read().decode(errors="replace").strip()
    if se:
        print("stderr:", se[:500], file=sys.stderr)
    _, so2, se2 = ssh.exec_command(
        "cd /home/unitree/go2_visual_dashboard && bash scripts/nx_start_dashboard.sh"
    )
    code = so2.channel.recv_exit_status()
    tail = so2.read().decode(errors="replace")
    print(tail[-2800:].rstrip())
    err2 = se2.read().decode(errors="replace").strip()
    if err2:
        print("restart stderr:", err2[-800:], file=sys.stderr)
    ssh.close()
    return 0 if code == 0 else code


if __name__ == "__main__":
    raise SystemExit(main())

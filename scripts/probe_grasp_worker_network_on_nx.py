#!/usr/bin/env python3
"""Esegue sulla Jetson (via SSH come deploy) ping/curl verso IP candidati per il worker grasp.

Uso (PC sulla LAN Unitree):
  python scripts/probe_grasp_worker_network_on_nx.py
  python scripts/probe_grasp_worker_network_on_nx.py 192.168.123.4 8765

Env: GO2_NX_HOST, GO2_NX_USER, GO2_NX_PASSWORD (stessi di deploy_dashboard_to_nx.py).
"""
from __future__ import annotations

import os
import shlex
import sys

import paramiko


def nx_host() -> str:
    return (os.environ.get("GO2_NX_HOST") or "192.168.123.18").strip() or "192.168.123.18"


def nx_user() -> str:
    return (os.environ.get("GO2_NX_USER") or "unitree").strip() or "unitree"


def nx_password() -> str:
    return os.environ.get("GO2_NX_PASSWORD") or "123"


def main() -> int:
    worker_ip = (sys.argv[1] if len(sys.argv) > 1 else "192.168.123.4").strip()
    worker_port = (sys.argv[2] if len(sys.argv) > 2 else "8765").strip()
    alt = os.environ.get("GO2_PROBE_ALT_WORKER_IP", "172.20.192.1").strip()

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"[probe] SSH {nx_user()}@{nx_host()} …")
    ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=30)

    def run(label: str, cmd: str) -> None:
        print(f"\n--- {label} ---")
        _, stdout, stderr = ssh.exec_command(cmd)
        stdout.channel.recv_exit_status()
        so = stdout.read().decode(errors="replace")
        se = stderr.read().decode(errors="replace")
        if so.strip():
            print(so.rstrip())
        if se.strip():
            print("stderr:", se.strip()[:400])

    run(f"ping {worker_ip}", f"ping -c 2 -W 2 {shlex.quote(worker_ip)} 2>&1 || true")
    run(f"ping {alt}", f"ping -c 2 -W 2 {shlex.quote(alt)} 2>&1 || true")
    url = f"http://{worker_ip}:{worker_port}/health"
    run(f"curl {url}", f"curl -sS --connect-timeout 3 -m 5 {shlex.quote(url)} 2>&1 || echo '(curl failed)'")

    ssh.close()
    print("\n[probe] done (exit 0 — risultati sopra sono solo diagnostici)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
SSH sulla NX: stato macchina (RAM, CPU, zombie, top processi, processi dashboard).

Esegue lo script locale ``scripts/nx_machine_diag.sh`` sulla Jetson via ``bash -s`` (non richiede il file sul disco remoto).

Uso dalla root del repo:
  python scripts/probe_nx_machine.py

Env: GO2_NX_HOST, GO2_NX_USER, GO2_NX_PASSWORD (stessi di deploy).
"""
from __future__ import annotations

import sys
from pathlib import Path

import paramiko

from deploy_dashboard_to_nx import nx_host, nx_password, nx_user

_REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    host = nx_host()
    print(f"[nx_machine] SSH {nx_user()}@{host} …")
    script_path = _REPO / "scripts" / "nx_machine_diag.sh"
    if not script_path.is_file():
        print("Missing", script_path, file=sys.stderr)
        return 3
    body = script_path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, username=nx_user(), password=nx_password(), timeout=45)
    except Exception as exc:
        print("SSH failed:", exc, file=sys.stderr)
        return 2

    stdin, stdout, stderr = ssh.exec_command("bash -s", timeout=120)
    stdin.write(body)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    print(out, end="" if out.endswith("\n") else "\n")
    if err.strip():
        print("--- stderr ---", file=sys.stderr)
        print(err, file=sys.stderr, end="")
    code = stdout.channel.recv_exit_status()
    ssh.close()
    return int(code) if code else 0


if __name__ == "__main__":
    raise SystemExit(main())

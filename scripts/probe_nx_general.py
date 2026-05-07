#!/usr/bin/env python3
"""Probe SSH sulla Jetson: rete, USB, video, dashboard (nessun push file)."""
from __future__ import annotations

import sys

import paramiko

from deploy_dashboard_to_nx import REMOTE_BASE, _remote_run_probe, nx_host, nx_password, nx_user


def main() -> int:
    host = nx_host()
    print(f"[probe] {nx_user()}@{host} …")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, username=nx_user(), password=nx_password(), timeout=45)
    except Exception as exc:
        print("SSH failed:", exc)
        return 2
    stdin, stdout, stderr = ssh.exec_command(f"test -x {REMOTE_BASE}/scripts/nx_peripheral_probe.sh && echo OK || echo MISSING")
    if "OK" not in stdout.read().decode():
        print("Script probe non presente sulla NX — esegui prima: python scripts/deploy_dashboard_to_nx.py")
        ssh.close()
        return 3
    _remote_run_probe(ssh)
    ssh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

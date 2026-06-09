#!/usr/bin/env python3
"""Reset Orbbec UVC + riavvio dashboard D1 jog (5053) sulla NX."""
from __future__ import annotations

import os
import time

import paramiko

HOST = os.environ.get("GO2_NX_HOST", "192.168.123.18")
BASE = "/home/unitree/go2_visual_dashboard"


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        HOST,
        username=os.environ.get("GO2_NX_USER", "unitree"),
        password=os.environ.get("GO2_NX_PASSWORD", "123"),
        timeout=30,
    )

    print("=== Reset Orbbec (UVC) ===")
    _, o, e = ssh.exec_command(f"bash {BASE}/scripts/orbbec_reset_camera.sh", timeout=60)
    print((o.read() or e.read()).decode(errors="replace").strip())
    print("exit", o.channel.recv_exit_status())

    time.sleep(1)
    print("\n=== Riavvio dashboard D1 jog (5053) ===")
    _, o, _ = ssh.exec_command(
        f"bash {BASE}/scripts/nx_stop_operator_dashboard.sh; "
        f"bash {BASE}/scripts/nx_start_d1_jog.sh",
        timeout=90,
    )
    print(o.read().decode(errors="replace").strip())
    print("exit", o.channel.recv_exit_status())

    time.sleep(2)
    print("\n=== Verifica ===")
    _, o, _ = ssh.exec_command(
        'curl -s -o /dev/null -w "health %{http_code}\\n" http://127.0.0.1:5053/api/health',
        timeout=15,
    )
    print(o.read().decode(errors="replace").strip())
    _, o, _ = ssh.exec_command(
        "curl -s http://127.0.0.1:5053/api/orbbec/probe | python3 -m json.tool 2>/dev/null | head -20",
        timeout=45,
    )
    print(o.read().decode(errors="replace").strip())

    ssh.close()
    print(f"\nApri: http://{HOST}:5053/")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Riavvia Hermes sulla NX (5054) via SSH."""
from __future__ import annotations

import os
import time

import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
host = os.environ.get("GO2_NX_HOST", "192.168.123.18")
ssh.connect(
    host,
    username=os.environ.get("GO2_NX_USER", "unitree"),
    password=os.environ.get("GO2_NX_PASSWORD", "123"),
    timeout=30,
)
_, o, _ = ssh.exec_command(
    "bash /home/unitree/go2_visual_dashboard/scripts/nx_start_hermes.sh",
    timeout=90,
)
print(o.read().decode(errors="replace"))
print("exit", o.channel.recv_exit_status())
time.sleep(2)
_, o, _ = ssh.exec_command(
    f'curl -s -o /dev/null -w "health %{{http_code}}\\n" http://127.0.0.1:5054/api/hermes/health',
    timeout=15,
)
print(o.read().decode(errors="replace"))
ssh.close()
print(f"\nApri: http://{host}:5054/")

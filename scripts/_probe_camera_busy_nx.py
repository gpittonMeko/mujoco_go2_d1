#!/usr/bin/env python3
import os
import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("192.168.123.18", username="unitree", password=os.environ.get("GO2_NX_PASSWORD", "123"), timeout=25)
cmds = [
    "fuser -v /dev/video4 /dev/video2 2>&1 || true",
    "lsof /dev/video4 2>/dev/null | head -10",
    "pgrep -af 'serve_|python3.*dashboard'",
    "bash -lc 'cd /home/unitree/go2_visual_dashboard && export GO2_LOCAL=1 GO2_REALSENSE_COLOR_BACKEND=pyrs GO2_REALSENSE_STREAMS=color PYTHONPATH=. && python3 scripts/_test_pyrs_once.py'",
]
for c in cmds:
    print("\n===", c[:70], "===")
    _, o, _ = ssh.exec_command(c, timeout=90)
    print(o.read().decode(errors="replace"))
ssh.close()

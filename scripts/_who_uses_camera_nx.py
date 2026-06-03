#!/usr/bin/env python3
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from deploy_d1_jog_to_nx import nx_host, nx_password, nx_user
import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=25)
cmds = [
    "fuser -v /dev/video* 2>&1",
    "ps aux | grep -E 'realsense|video|serve_|dashboard|camera' | grep -v grep",
    "ss -tlnp | grep -E '5052|5053'",
]
for c in cmds:
    _, o, _ = ssh.exec_command(c, timeout=20)
    print("===", c, "===")
    print(o.read().decode(errors="replace"))
ssh.close()

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
    "ls -la /dev/video* 2>/dev/null; lsusb | grep 8086",
    "which rs-enumerate-devices; dpkg -l | grep -i realsense | head -5",
    "pip3 show pyrealsense2 2>/dev/null | head -5",
    "python3 -c 'import importlib.util; print(importlib.util.find_spec(\"pyrealsense2\"))'",
]
for c in cmds:
    _, o, _ = ssh.exec_command(c, timeout=30)
    print("===", c, "===")
    print(o.read().decode(errors="replace"))
ssh.close()

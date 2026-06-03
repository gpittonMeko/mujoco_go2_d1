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
_, o, _ = ssh.exec_command(
    "find /opt/ros -name 'pyrealsense2*' 2>/dev/null | head -20; "
    "find /usr -name 'pyrealsense2*' 2>/dev/null | head -10; "
    "ls /opt/ros/noetic/lib/python3/dist-packages/ 2>/dev/null | head -30",
    timeout=60,
)
print(o.read().decode(errors="replace"))
ssh.close()

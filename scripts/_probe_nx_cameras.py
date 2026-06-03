#!/usr/bin/env python3
import paramiko
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from deploy_d1_jog_to_nx import nx_host, nx_user, nx_password, REMOTE_BASE

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=25)
script = r"""
ls -l /dev/video* 2>/dev/null || true
echo '--- v4l2 ---'
v4l2-ctl --list-devices 2>/dev/null || true
echo '--- usb ---'
lsusb 2>/dev/null | head -20
echo '--- cv2 ---'
python3 -c "import cv2; print('cv2', cv2.__version__)" 2>/dev/null || echo no_cv2
"""
_, o, _ = ssh.exec_command(f"bash -lc {repr(script)}", timeout=30)
print(o.read().decode(errors="replace"))
ssh.close()

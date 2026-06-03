#!/usr/bin/env python3
import os
from pathlib import Path
import paramiko

REMOTE = "/home/unitree/go2_visual_dashboard"
REPO = Path(__file__).resolve().parent.parent
FILES = [
    "scripts/nx_install_yolo_vision.sh",
    "scripts/nx_d1_jog_env.sh",
    "go2_dashboard/d1_jog/vision_yolo.py",
    "scripts/box_object_detector.py",
]

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("192.168.123.18", username="unitree", password=os.environ.get("GO2_NX_PASSWORD", "123"), timeout=25)
sftp = ssh.open_sftp()
for rel in FILES:
    sftp.put(str(REPO / rel), f"{REMOTE}/{rel}")
    print("pushed", rel)
sftp.close()
_, o, _ = ssh.exec_command(f"chmod +x {REMOTE}/scripts/nx_install_yolo_vision.sh && bash {REMOTE}/scripts/nx_install_yolo_vision.sh", timeout=300)
print(o.read().decode(errors="replace"))
print("install exit", o.channel.recv_exit_status())
_, o, _ = ssh.exec_command(f"bash {REMOTE}/scripts/nx_start_d1_jog.sh", timeout=90)
print(o.read().decode(errors="replace"))
_, o, _ = ssh.exec_command("curl -s http://127.0.0.1:5053/api/vision/detect/plan", timeout=60)
print(o.read().decode(errors="replace")[:1500])
ssh.close()

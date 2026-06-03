#!/usr/bin/env python3
import os
from pathlib import Path
import paramiko

REPO = Path(__file__).resolve().parent.parent
REMOTE = "/home/unitree/go2_visual_dashboard"
FILES = [
    "go2_dashboard/d1_jog/vision_page.py",
    "go2_dashboard/realsense_pyrs.py",
    "go2_dashboard/cameras.py",
    "templates/vision_dashboard.html",
    "scripts/nx_start_d1_jog.sh",
]
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(
    os.environ.get("GO2_NX_HOST", "192.168.123.18"),
    username="unitree",
    password=os.environ.get("GO2_NX_PASSWORD", "123"),
    timeout=25,
)
sftp = ssh.open_sftp()
for rel in FILES:
    sftp.put(str(REPO / rel), f"{REMOTE}/{rel}")
    print("pushed", rel)
sftp.close()
_, o, _ = ssh.exec_command(f"chmod +x {REMOTE}/scripts/nx_start_d1_jog.sh && bash {REMOTE}/scripts/nx_start_d1_jog.sh", timeout=120)
print(o.read().decode(errors="replace"))
print("exit", o.channel.recv_exit_status())
for cmd in [
    "curl -s http://127.0.0.1:5053/api/vision/camera/status",
    'curl -s -o /tmp/s.jpg -w "snap %{http_code} %{size_download}\\n" http://127.0.0.1:5053/api/vision/camera/snapshot.jpg',
    'timeout 3 curl -s -o /tmp/d.bin -w "mjpeg %{http_code} %{size_download}\\n" http://127.0.0.1:5053/stream/vision/detect.mjpg; ls -la /tmp/d.bin 2>/dev/null',
]:
    _, o, _ = ssh.exec_command(cmd, timeout=20)
    print(o.read().decode(errors="replace"))
ssh.close()

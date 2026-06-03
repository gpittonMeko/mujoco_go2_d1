#!/usr/bin/env python3
"""Libera camera e testa video4 V4L + pyrealsense2 RGB."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from deploy_d1_jog_to_nx import REMOTE_BASE, nx_host, nx_password, nx_user
import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=25)
ssh.exec_command(
    "pkill -f serve_dashboard_modular.py 2>/dev/null; "
    "pkill -f nx_dashboard_supervise 2>/dev/null; "
    "pkill -f serve_d1_jog 2>/dev/null; "
    "sleep 3; fuser -v /dev/video* 2>&1",
    timeout=30,
)
_, o, _ = ssh.exec_command(f"bash {REMOTE_BASE}/scripts/_pyrs_rgb_test_nx.sh", timeout=90)
print("=== pyrealsense ===")
print(o.read().decode(errors="replace"))

INLINE = r"""
import cv2
def chroma(fr):
    if fr is None or fr.ndim != 3: return 0.0
    return float(cv2.mean(cv2.absdiff(fr[:,:,0], fr[:,:,1]))[0] + cv2.mean(cv2.absdiff(fr[:,:,1], fr[:,:,2]))[0])
for idx in [2, 4, 5]:
    for fc in ["YUYV", "MJPG", ""]:
        cap = cv2.VideoCapture("/dev/video%d" % idx, cv2.CAP_V4L2)
        if not cap.isOpened():
            print("video%d %s: no open" % (idx, fc or "def"))
            continue
        if fc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fc))
        cap.set(3, 640); cap.set(4, 480)
        bc = 0
        for _ in range(15):
            ok, fr = cap.read()
            if ok and fr is not None:
                bc = max(bc, chroma(fr))
        cap.release()
        print("video%d %s chroma=%.2f %s" % (idx, fc or "def", bc, "RGB" if bc>=2.5 else "gray"))
"""

sftp = ssh.open_sftp()
with sftp.file(f"{REMOTE_BASE}/scripts/_v4l_rgb_quick.py", "w") as f:
    f.write(INLINE.strip() + "\n")
sftp.close()
_, o, _ = ssh.exec_command(f"python3 {REMOTE_BASE}/scripts/_v4l_rgb_quick.py", timeout=90)
print("=== v4l ===")
print(o.read().decode(errors="replace"))
ssh.close()

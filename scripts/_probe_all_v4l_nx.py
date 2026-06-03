#!/usr/bin/env python3
"""Probe tutti i /dev/videoN sulla NX: nome sysfs, chroma, apertura."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from deploy_d1_jog_to_nx import REMOTE_BASE, nx_host, nx_password, nx_user

import paramiko

INLINE = r"""
import sys, os, glob
sys.path.insert(0, ".")
os.environ.pop("GO2_VIDEO_INDEX_6", None)
import cv2
import numpy as np

def chroma(fr):
    if fr is None or fr.ndim != 3 or fr.shape[2] < 3:
        return 0.0
    d0 = cv2.absdiff(fr[:,:,0], fr[:,:,1])
    d1 = cv2.absdiff(fr[:,:,1], fr[:,:,2])
    return float(cv2.mean(d0)[0] + cv2.mean(d1)[0])

for path in sorted(glob.glob("/dev/video*")):
    if not path.startswith("/dev/video"):
        continue
    idx = int(path.replace("/dev/video", ""))
    name = ""
    try:
        name = open("/sys/class/video4linux/video%d/name" % idx).read().strip()
    except Exception:
        pass
    cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
    if not cap.isOpened():
        print("video%d name=%r OPEN_FAIL" % (idx, name))
        continue
    try:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    except Exception:
        pass
    cap.set(3, 640)
    cap.set(4, 480)
    bc = 0.0
    bm = 0.0
    std = 0.0
    for _ in range(30):
        ok, fr = cap.read()
        if ok and fr is not None:
            bc = max(bc, chroma(fr))
            bm = max(bm, float(fr.max()))
            std = max(std, float(fr.std()))
    cap.release()
    kind = "RGB" if bc >= 2.5 else ("GRAY/IR" if bc < 0.5 else "weak_color")
    print("video%d name=%r chroma=%.2f max=%.0f std=%.1f kind=%s" % (idx, name, bc, bm, std, kind))
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=25)
ssh.exec_command("pkill -f serve_d1_jog_dashboard.py 2>/dev/null; sleep 2", timeout=15)
sftp = ssh.open_sftp()
with sftp.file(f"{REMOTE_BASE}/scripts/_v4l_all_probe.py", "w") as f:
    f.write(INLINE.strip() + "\n")
sftp.close()
_, o, e = ssh.exec_command(
    f"cd {REMOTE_BASE} && python3 scripts/_v4l_all_probe.py",
    timeout=120,
)
print(o.read().decode(errors="replace"))
if e.read().decode().strip():
    print("err:", e.read().decode()[:500])
ssh.close()

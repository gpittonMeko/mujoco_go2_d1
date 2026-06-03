#!/usr/bin/env python3
"""Trova nodo V4L RGB RealSense sulla NX (formati, chroma, esclusività)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from deploy_d1_jog_to_nx import REMOTE_BASE, nx_host, nx_password, nx_user

import paramiko

INLINE = r"""
import glob, os, subprocess, sys
sys.path.insert(0, ".")
import cv2
import numpy as np

def chroma(fr):
    if fr is None or fr.ndim != 3 or fr.shape[2] < 3:
        return 0.0
    return float(cv2.mean(cv2.absdiff(fr[:,:,0], fr[:,:,1]))[0] + cv2.mean(cv2.absdiff(fr[:,:,1], fr[:,:,2]))[0])

print("=== v4l2-ctl --list-devices ===")
try:
    subprocess.run(["v4l2-ctl", "--list-devices"], check=False)
except FileNotFoundError:
    print("(v4l2-ctl non installato)")

print("\n=== formats per video ===")
for idx in range(8):
    p = "/dev/video%d" % idx
    if not os.path.exists(p):
        continue
    print("\n---", p, "---")
    try:
        subprocess.run(["v4l2-ctl", "-d", p, "--list-formats-ext"], check=False)
    except FileNotFoundError:
        break

FOURCCS = ["", "MJPG", "YUYV", "RGB3", "GREY"]

def try_node(idx, fourcc_name):
    cap = cv2.VideoCapture("/dev/video%d" % idx, cv2.CAP_V4L2)
    if not cap.isOpened():
        return None
    if fourcc_name:
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc_name))
        except Exception:
            pass
    for w, h in [(640, 480), (1280, 720), (1920, 1080)]:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        bc = bm = 0.0
        ok_n = 0
        for _ in range(20):
            ok, fr = cap.read()
            if ok and fr is not None and fr.size:
                ok_n += 1
                bc = max(bc, chroma(fr))
                bm = max(bm, float(fr.max()))
        if ok_n >= 3:
            cap.release()
            return (fourcc_name or "def", w, h, bc, bm, ok_n)
    cap.release()
    return None

print("\n=== probe esclusivo (ogni nodo, poi release) ===")
best = None
for idx in range(8):
    p = "/dev/video%d" % idx
    if not os.path.exists(p):
        continue
    for fc in FOURCCS:
        r = try_node(idx, fc)
        if r is None:
            continue
        kind = "RGB" if r[3] >= 2.5 else "IR/gray"
        print("video%d fourcc=%s %dx%d chroma=%.2f max=%.0f frames=%d %s" % (idx, r[0], r[1], r[2], r[3], r[4], r[5], kind))
        score = r[3] * 10 + r[4] * 0.01
        if best is None or score > best[0]:
            best = (score, idx, r)

if best:
    print("\nBEST_RGB_CANDIDATE: video%d chroma=%.2f fourcc=%s %dx%d" % (best[1], best[2][3], best[2][0], best[2][1], best[2][2]))
else:
    print("\nBEST_RGB_CANDIDATE: none")

print("\n=== pyrealsense2 ===")
try:
    import pyrealsense2 as rs
    ctx = rs.context()
    for d in ctx.query_devices():
        print("device:", d.get_info(rs.camera_info.NAME))
    print("pyrealsense2 OK")
except Exception as e:
    print("pyrealsense2:", e)
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=25)
ssh.exec_command(
    "pkill -f serve_d1_jog_dashboard.py 2>/dev/null; pkill -f serve_d1_jog 2>/dev/null; sleep 2",
    timeout=20,
)
sftp = ssh.open_sftp()
with sftp.file(f"{REMOTE_BASE}/scripts/_rgb_find.py", "w") as f:
    f.write(INLINE.strip() + "\n")
sftp.close()
_, o, e = ssh.exec_command(f"cd {REMOTE_BASE} && python3 scripts/_rgb_find.py 2>&1", timeout=180)
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err.strip():
    print("stderr tail:", err[-800:])
ssh.close()

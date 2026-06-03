#!/usr/bin/env python3
"""Probe RealSense V4L nodes on NX — quale ha RGB (chroma > 0)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from deploy_d1_jog_to_nx import nx_host, nx_password, nx_user, REMOTE_BASE

import paramiko

script = f"""
cd {REMOTE_BASE} && . scripts/nx_d1_jog_env.sh
python3 << 'PY'
import os, sys
sys.path.insert(0, ".")
from go2_dashboard.cameras import (
    _enumerate_v4l_usb_bindings,
    _USB_IDS_REALSENSE,
    _frame_channel_chroma_bgr,
    _frame_looks_like_rgb_color,
    _v4l_path,
    _try_set_uvc_mjpeg_fourcc,
    cv2,
)
rows = _enumerate_v4l_usb_bindings()
rs = sorted({{i for i,v,p in rows if (v,p) in _USB_IDS_REALSENSE}})
print("RealSense indices:", rs)
for idx in rs:
    cap = cv2.VideoCapture(_v4l_path(idx), cv2.CAP_V4L2)
    if not cap.isOpened():
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
    if not cap.isOpened():
        print(idx, "OPEN_FAIL")
        continue
    _try_set_uvc_mjpeg_fourcc(cap)
    cap.set(3, 640); cap.set(4, 480)
    best_chroma = 0.0
    best_max = 0.0
    rgb_ok = False
    for n in range(20):
        ok, fr = cap.read()
        if not ok or fr is None:
            continue
        c = _frame_channel_chroma_bgr(fr)
        m = float(fr.max())
        if c > best_chroma:
            best_chroma = c
        if m > best_max:
            best_max = m
        if _frame_looks_like_rgb_color(fr):
            rgb_ok = True
    cap.release()
    print(f"video{{idx}}: chroma={{best_chroma:.2f}} max={{best_max:.1f}} rgb_ok={{rgb_ok}}")
from go2_dashboard.cameras import usb_auto_v4l_mapping, _v4l_index_for_logical_camera
print("usb_auto_map", usb_auto_v4l_mapping())
print("logical 6 ->", _v4l_index_for_logical_camera(6))
PY
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=25)
_, o, e = ssh.exec_command(f"bash -lc {repr(script)}", timeout=90)
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err:
    print("stderr:", err)
ssh.close()

#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from deploy_d1_jog_to_nx import REMOTE_BASE, nx_host, nx_password, nx_user

import paramiko

INLINE = r"""
import sys
sys.path.insert(0, ".")
from go2_dashboard.cameras import (
    _enumerate_v4l_usb_bindings,
    _USB_IDS_REALSENSE,
    _frame_channel_chroma_bgr,
    _frame_looks_like_rgb_color,
    _v4l_path,
    _try_set_uvc_mjpeg_fourcc,
    usb_auto_v4l_mapping,
    _v4l_index_for_logical_camera,
    cv2,
)
rows = _enumerate_v4l_usb_bindings()
rs = sorted({i for i, v, p in rows if (v, p) in _USB_IDS_REALSENSE})
print("RealSense indices:", rs)
for idx in rs:
    cap = cv2.VideoCapture(_v4l_path(idx), cv2.CAP_V4L2)
    if not cap.isOpened():
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
    if not cap.isOpened():
        print(idx, "OPEN_FAIL")
        continue
    _try_set_uvc_mjpeg_fourcc(cap)
    cap.set(3, 640)
    cap.set(4, 480)
    bc = 0.0
    bm = 0.0
    ro = False
    for _ in range(25):
        ok, fr = cap.read()
        if ok and fr is not None:
            c = _frame_channel_chroma_bgr(fr)
            m = float(fr.max())
            bc = max(bc, c)
            bm = max(bm, m)
            if _frame_looks_like_rgb_color(fr):
                ro = True
    cap.release()
    print("video%d: chroma=%.2f max=%.1f rgb_ok=%s" % (idx, bc, bm, ro))
print("usb_auto_map", usb_auto_v4l_mapping())
print("logical_6", _v4l_index_for_logical_camera(6))
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=25)
sftp = ssh.open_sftp()
remote = f"{REMOTE_BASE}/scripts/_rs_probe_tmp.py"
with sftp.file(remote, "w") as f:
    f.write(INLINE.strip() + "\n")
sftp.close()
_, o, e = ssh.exec_command(
    f"cd {REMOTE_BASE} && . scripts/nx_d1_jog_env.sh && python3 scripts/_rs_probe_tmp.py",
    timeout=120,
)
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err.strip():
    print("stderr:", err)
ssh.close()

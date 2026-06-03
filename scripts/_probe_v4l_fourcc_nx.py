#!/usr/bin/env python3
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from deploy_d1_jog_to_nx import REMOTE_BASE, nx_host, nx_password, nx_user
import paramiko

INLINE = r"""
import cv2, glob
def chroma(fr):
    if fr is None or fr.ndim != 3: return 0.0
    return float(cv2.mean(cv2.absdiff(fr[:,:,0], fr[:,:,1]))[0] + cv2.mean(cv2.absdiff(fr[:,:,1], fr[:,:,2]))[0])
for idx in range(6):
    for fourcc_name in ["MJPG", "YUYV", ""]:
        cap = cv2.VideoCapture("/dev/video%d" % idx, cv2.CAP_V4L2)
        if not cap.isOpened():
            continue
        if fourcc_name:
            try:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc_name))
            except Exception:
                pass
        cap.set(3, 640)
        cap.set(4, 480)
        ok_any = False
        bc = 0.0
        for _ in range(12):
            ok, fr = cap.read()
            if ok and fr is not None:
                ok_any = True
                bc = max(bc, chroma(fr))
        cap.release()
        if ok_any:
            print("video%d fourcc=%s chroma=%.2f shape_ok" % (idx, fourcc_name or "default", bc))
            break
    else:
        print("video%d all_fail" % idx)
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=25)
sftp = ssh.open_sftp()
with sftp.file(f"{REMOTE_BASE}/scripts/_fc_probe.py", "w") as f:
    f.write(INLINE.strip() + "\n")
sftp.close()
_, o, _ = ssh.exec_command(f"cd {REMOTE_BASE} && python3 scripts/_fc_probe.py", timeout=90)
print(o.read().decode())
ssh.close()

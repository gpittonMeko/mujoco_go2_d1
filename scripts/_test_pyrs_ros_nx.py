#!/usr/bin/env python3
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from deploy_d1_jog_to_nx import REMOTE_BASE, nx_host, nx_password, nx_user
import paramiko

INLINE = r"""
source /opt/ros/noetic/setup.bash 2>/dev/null || true
export PYTHONPATH="/opt/ros/noetic/lib/python3/dist-packages:${PYTHONPATH}"
echo PYTHONPATH=$PYTHONPATH
python3 << 'PY'
import sys
try:
    import pyrealsense2 as rs
    print("pyrealsense2 import OK", rs.__file__)
    ctx = rs.context()
    devs = ctx.query_devices()
    print("devices", len(devs))
    for d in devs:
        print(" ", d.get_info(rs.camera_info.NAME))
    if len(devs) < 1:
        raise SystemExit(1)
    cfg = rs.config()
    cfg.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 15)
    pipe = rs.pipeline()
    prof = pipe.start(cfg)
    for i in range(15):
        frames = pipe.wait_for_frames(5000)
        c = frames.get_color_frame()
        if not c:
            continue
        import numpy as np
        img = np.asanyarray(c.get_data())
        if img.ndim == 3 and img.shape[2] >= 3:
            d0 = np.abs(img[:,:,0].astype(int) - img[:,:,1].astype(int))
            chroma = float(d0.mean() + np.abs(img[:,:,1].astype(int)-img[:,:,2].astype(int)).mean())
            print("frame", i, "shape", img.shape, "max", img.max(), "chroma", round(chroma,2))
    pipe.stop()
    print("RGB_STREAM_OK")
except Exception as e:
    print("FAIL", type(e).__name__, e)
    import traceback
    traceback.print_exc()
PY
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=25)
ssh.exec_command("pkill -f serve_d1_jog_dashboard.py 2>/dev/null; sleep 2", timeout=15)
_, o, e = ssh.exec_command(f"bash -lc {repr(INLINE)}", timeout=120)
print(o.read().decode(errors="replace"))
print(e.read().decode(errors="replace")[-500:])
ssh.close()

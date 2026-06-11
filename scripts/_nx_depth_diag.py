import paramiko
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "/home/unitree/go2_visual_dashboard"
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.123.18", username="unitree", password="123", timeout=12)
cmd = f"""
cd {BASE} && python3 - <<'PY'
import numpy as np
import sys
sys.path.insert(0,'scripts')
from go2_dashboard.orbbec_wrist_grasp import capture_aligned, _depth_median_m, _filter_wrist_detection
from box_object_detector import detect_box_object

cap = capture_aligned()
print('cap', cap.get('ok'), cap.get('reason'))
if not cap.get('ok'):
    raise SystemExit
color = cap['color_bgr']
depth = cap['depth_u16']
intr = cap['intrinsics']
scale = cap['depth_scale_mm']
print('depth shape', depth.shape, 'scale_mm', scale, 'nonzero', int((depth>0).sum()))
det = _filter_wrist_detection(detect_box_object(color), intr)
print('det', det.get('ok'), det.get('reason'), det.get('bbox_xyxy'))
if det.get('bbox_xyxy'):
    for shrink in (0.0, 0.15, 0.25, 0.35):
        dm = _depth_median_m(depth, scale, det['bbox_xyxy'], shrink=shrink)
        print('shrink', shrink, dm)
    bb = det['bbox_xyxy']
    x0,y0,x1,y1 = [int(v) for v in bb]
    roi = depth[max(0,y0):min(depth.shape[0],y1), max(0,x0):min(depth.shape[1],x1)]
    print('roi nonzero', int((roi>0).sum()), 'roi size', roi.size)
    # center pixel depth
    cx,cy = det.get('bbox_center_px') or [0,0]
    ci,cj = int(round(cy)), int(round(cx))
    if 0<=cj<depth.shape[1] and 0<=ci<depth.shape[0]:
        v = int(depth[ci,cj])
        print('center depth raw', v, 'm', v*scale/1000.0)
PY
"""
_i, o, e = c.exec_command(cmd, timeout=60)
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err:
    print("stderr", err[:500])
c.close()

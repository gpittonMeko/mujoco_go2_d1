#!/usr/bin/env python3
import json
import os
import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("192.168.123.18", username="unitree", password=os.environ.get("GO2_NX_PASSWORD", "123"), timeout=25)

cmds = [
    "curl -s http://127.0.0.1:5053/api/vision/detector/status",
    "curl -s http://127.0.0.1:5053/api/vision/detect/last",
    "curl -s http://127.0.0.1:5053/api/vision/detect/plan",
    "grep VISION_DETECT\\|GO2_YOLO\\|CLASSIC /home/unitree/go2_visual_dashboard/scripts/nx_d1_jog_env.sh",
    f"""bash -lc 'cd /home/unitree/go2_visual_dashboard && export GO2_LOCAL=1 PYTHONPATH=. GO2_YOLO_MODEL=/home/unitree/go2_visual_dashboard/models/yolo11n.pt GO2_CLASSIC_BOX_FALLBACK=0 && python3 -c "
import urllib.request, json
from go2_dashboard import cameras
from go2_dashboard.d1_jog import vision_detect
cameras.CAMERA_CACHE.start(6)
import time; time.sleep(2)
jpg = cameras.CAMERA_CACHE.get_jpeg(6, wait_s=5)
print(\"jpg\", len(jpg) if jpg else None)
cv2 = cameras.cv2
frame = vision_detect._frame_from_jpeg(jpg, cv2)
det = vision_detect.run_detect_frame(frame, cv2)
print(json.dumps({{k:v for k,v in vision_detect.plan_summary(det).items() if k != \"_\"}}, indent=2))
" 2>&1'""",
]
for c in cmds:
    print("\n===", c[:70], "===")
    _, o, _ = ssh.exec_command(c, timeout=120)
    print(o.read().decode(errors="replace")[:2500])
ssh.close()

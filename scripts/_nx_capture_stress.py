import paramiko
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.123.18", username="unitree", password="123", timeout=12)
cmd = r"""
cd /home/unitree/go2_visual_dashboard && python3 - <<'PY'
import time
from go2_dashboard.orbbec_wrist_grasp import capture_aligned
from go2_dashboard.cameras import CAMERA_CACHE
CAMERA_CACHE.start(0)
time.sleep(1.0)
fails = []
for i in range(8):
    t0 = time.time()
    cap = capture_aligned()
    dt = (time.time() - t0) * 1000
    r = cap.get('reason') if not cap.get('ok') else 'ok'
    print(f"#{i+1} {dt:.0f}ms {r} holder={cap.get('holder')}")
    if not cap.get('ok'):
        fails.append(r)
    time.sleep(0.3)
print('failures', fails, 'rate', len(fails), '/8')
PY
"""
_i, o, e = c.exec_command(cmd, timeout=90)
print(o.read().decode(errors="replace"))
if e.read().decode(errors="replace").strip():
    print("ERR", e.read())
c.close()

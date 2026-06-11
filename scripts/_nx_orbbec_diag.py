import json
import sys
import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "/home/unitree/go2_visual_dashboard"
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.123.18", username="unitree", password="123", timeout=12)


def run(cmd, tmo=45):
    _i, o, e = c.exec_command(cmd, timeout=tmo)
    out = o.read().decode(errors="replace").strip()
    err = e.read().decode(errors="replace").strip()
    print("$ " + cmd)
    print(out[:5000] if out else "(empty)")
    if err:
        print("[stderr] " + err[:800])
    print("---")
    return out


run("curl -s -m 6 http://127.0.0.1:5052/api/health")
run("curl -s -m 12 http://127.0.0.1:5052/api/cameras/status | python3 -c \"import sys,json;d=json.load(sys.stdin);s=d.get('camera_summary',{});print('log0',s.get('0'));print('idx',d.get('v4l_index_by_logical'));print('lock',d.get('orbbec_logical_0_probe_debug'))\"")
run(f"cat {BASE}/data/grasp_debug_wrist_orbbec.json 2>/dev/null | python3 -c \"import sys,json; d=json.load(sys.stdin); print(json.dumps({{k:d.get(k) for k in ('step','detection_ok','saved_at')}}, indent=2)); det=d.get('detection',{{}}); print('det',{{k:det.get(k) for k in ('ok','reason','backend')}})\" 2>/dev/null || echo NO_DEBUG")
run(f"ls -la {BASE}/data/grasp_debug_wrist_orbbec.jpg 2>/dev/null; ls -la /tmp/go2_orbbec*.lock 2>/dev/null || echo no_orbbec_lock")
run(
    f"cd {BASE} && python3 - <<'PY'\n"
    "import json\n"
    "try:\n"
    " from go2_dashboard.orbbec_wrist_grasp import capture_aligned, available\n"
    " print('available', available())\n"
    " cap=capture_aligned()\n"
    " print('capture', {k:cap.get(k) for k in ('ok','reason','detail') if k in cap})\n"
    " if cap.get('ok'):\n"
    "  c=cap.get('color_bgr')\n"
    "  print('color', None if c is None else c.shape)\n"
    "except Exception as e:\n"
    " print('ERR', repr(e))\n"
    "PY"
)
run("ps aux | grep -E 'serve_dashboard|d1_sdk|orbbec|5052|5053' | grep -v grep | head -20")
run("dmesg 2>/dev/null | tail -15 | grep -iE 'usb|video|orbbec|uvc' || echo no_dmesg_usb")
c.close()

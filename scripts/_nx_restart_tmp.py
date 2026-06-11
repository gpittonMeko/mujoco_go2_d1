import paramiko
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "/home/unitree/go2_visual_dashboard"


def connect():
    for attempt in range(1, 12):
        try:
            cc = paramiko.SSHClient()
            cc.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cc.connect("192.168.123.18", username="unitree", password="123",
                       timeout=10, banner_timeout=12, auth_timeout=12)
            print(f"[ssh] connected on attempt {attempt}")
            return cc
        except Exception as ex:  # noqa
            print(f"[ssh] attempt {attempt} failed: {ex}")
            time.sleep(3)
    return None


c = connect()
if c is None:
    print("SSH_UNREACHABLE")
    sys.exit(2)


def run(cmd, tmo=40):
    _i, o, e = c.exec_command(cmd, timeout=tmo)
    try:
        out = o.read().decode(errors="replace")
        err = e.read().decode(errors="replace")
    except Exception as ex:  # noqa
        out, err = f"[timeout {ex}]", ""
    print("$ " + cmd)
    print(out)
    if err.strip():
        print("[stderr] " + err)
    print("-" * 60)
    return out


run("echo '== supervisors before =='; pgrep -af nx_dashboard_supervise.sh; pgrep -af serve_dashboard_lite.py || echo none")
# Hard kill all main dashboard supervisors + instances
run("pkill -9 -f nx_dashboard_supervise.sh 2>/dev/null; pkill -9 -f serve_dashboard_lite.py 2>/dev/null; fuser -k 5052/tcp 2>/dev/null; sleep 3; "
    "echo '== after kill =='; pgrep -af 'nx_dashboard_supervise.sh|serve_dashboard_lite.py' || echo NONE_LEFT")
# Start exactly one fresh supervisor (sources env with =6)
run(f"cd {BASE} && setsid bash -c 'nohup bash scripts/nx_dashboard_supervise.sh >> dashboard_supervise.log 2>&1' </dev/null >/dev/null 2>&1 & disown; echo launched")
print("waiting 20s for boot...")
time.sleep(20)
run("curl -s -m 6 -o /dev/null -w 'health=%{http_code}\\n' http://127.0.0.1:5052/api/health")
run("curl -s -m 8 'http://127.0.0.1:5052/api/cameras/status' 2>/dev/null | python3 -c \"import sys,json;d=json.load(sys.stdin);c=d.get('cameras',{});print('log0:',{k:c.get('0',{}).get(k) for k in ['available','color_chroma','rgb_like','stream_kind','error']});print('idx:',d.get('v4l_index_by_logical'))\" 2>&1")
c.close()

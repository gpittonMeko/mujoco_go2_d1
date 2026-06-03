#!/usr/bin/env python3
"""Ferma tutto, testa pyrs, riavvia 5053."""
import os
import paramiko

REMOTE = "/home/unitree/go2_visual_dashboard"
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("192.168.123.18", username="unitree", password=os.environ.get("GO2_NX_PASSWORD", "123"), timeout=25)

def run(cmd, timeout=90):
    print("\n$", cmd[:80])
    _, o, e = ssh.exec_command(cmd, timeout=timeout)
    out = o.read().decode(errors="replace")
    err = e.read().decode(errors="replace")
    code = o.channel.recv_exit_status()
    if out.strip():
        print(out.strip())
    if err.strip():
        print("stderr:", err.strip()[:500])
    print("exit", code)
    return code, out

run("pkill -f serve_d1_jog_dashboard.py; pkill -f serve_dashboard_modular.py; pkill -f nx_dashboard_supervise; sleep 2")
run("fuser -k /dev/video4 /dev/video2 2>/dev/null; sleep 2")
run(f"cd {REMOTE} && GO2_LOCAL=1 GO2_REALSENSE_COLOR_BACKEND=pyrs GO2_REALSENSE_STREAMS=color PYTHONPATH={REMOTE} python3 {REMOTE}/scripts/_test_pyrs_once.py", timeout=30)
run(f"bash {REMOTE}/scripts/nx_start_d1_jog.sh", timeout=90)
run("sleep 6 && curl -s http://127.0.0.1:5053/api/vision/camera/status")
run('curl -s -o /tmp/s.jpg -w "snap %{http_code} %{size_download}\\n" http://127.0.0.1:5053/api/vision/camera/snapshot.jpg')
ssh.close()

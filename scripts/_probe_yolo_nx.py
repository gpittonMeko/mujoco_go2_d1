#!/usr/bin/env python3
import os
import paramiko

host = os.environ.get("GO2_NX_HOST", "192.168.123.18")
pwd = os.environ.get("GO2_NX_PASSWORD", "123")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(host, username="unitree", password=pwd, timeout=25)
cmds = [
    "python3 -c 'import ultralytics; print(ultralytics.__version__)' 2>&1; echo exit=$?",
    "pip3 show ultralytics 2>&1 | head -8",
    "ls -la /home/unitree/go2_visual_dashboard/models/ 2>&1 || true",
    "grep -rh GO2_YOLO /home/unitree/go2_visual_dashboard/scripts/ 2>/dev/null | head -10",
    "find /home/unitree/go2_visual_dashboard -name '*.pt' -o -name '*.engine' 2>/dev/null | head -20",
    "curl -s http://127.0.0.1:5053/api/vision/detector/status 2>/dev/null | head -c 800",
]
for c in cmds:
    print("===", c, "===")
    _, o, e = ssh.exec_command(c, timeout=45)
    print(o.read().decode(errors="replace"))
    err = e.read().decode(errors="replace")
    if err.strip():
        print("stderr:", err[:300])
ssh.close()

#!/usr/bin/env python3
import os
import time
import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(
    os.environ.get("GO2_NX_HOST", "192.168.123.18"),
    username="unitree",
    password=os.environ.get("GO2_NX_PASSWORD", "123"),
    timeout=30,
)
_, o, _ = ssh.exec_command(
    "bash /home/unitree/go2_visual_dashboard/scripts/nx_start_d1_jog.sh",
    timeout=90,
)
print(o.read().decode(errors="replace"))
print("exit", o.channel.recv_exit_status())
time.sleep(3)
for cmd in [
    'curl -s -o /dev/null -w "health %{http_code}\n" http://127.0.0.1:5053/api/health',
    "curl -s http://127.0.0.1:5053/api/vision/camera/status | head -c 500",
]:
    _, o, _ = ssh.exec_command(cmd, timeout=15)
    print(o.read().decode(errors="replace"))
ssh.close()

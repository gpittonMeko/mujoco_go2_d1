#!/usr/bin/env python3
import paramiko
import time

HOST, USER, PW = "192.168.123.18", "unitree", "123"
BASE = "/home/unitree/go2_visual_dashboard"

SCRIPT = r"""
import os, sys, time
sys.path.insert(0, '/home/unitree/go2_visual_dashboard')
os.chdir('/home/unitree/go2_visual_dashboard')
from go2_dashboard.d1_jog import service

def fb():
    r = service.read_servo_deg(fast=True)
    return r.get('servo_deg') if r.get('ok') else None

print('enable power+motors')
service.enable_all(with_power=True)
time.sleep(0.8)
a0 = fb()
print('a0', a0)
sd = list(a0)
sd[0] += 12.0
r = service.jog_pose_deg(sd, mode=1)
print('jog', r.get('ok'), r.get('returncode'))
time.sleep(2.5)
a1 = fb()
print('a1', a1)
print('delta0', a1[0]-a0[0] if a1 else None)
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PW, timeout=20)
sftp = ssh.open_sftp()
with sftp.file(f"{BASE}/scripts/_diag_power.py", "w") as f:
    f.write(SCRIPT)
sftp.close()
_, o, _ = ssh.exec_command(
    f"cd {BASE} && . scripts/nx_d1_jog_env.sh && python3 scripts/_diag_power.py 2>/dev/null",
    timeout=50,
)
print(o.read().decode())
ssh.close()

#!/usr/bin/env python3
"""Test mode 0 vs 1 e stdout comando."""
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

service.enable_all()
time.sleep(0.6)
a0 = fb()
print('a0', a0)
for mode in (0, 1):
    sd = list(a0)
    sd[0] += 10.0 * (1 if mode == 1 else -1)  # opposite dirs to see any motion
    sd[0] = max(-135, min(135, sd[0]))
    r = service.jog_pose_deg(sd, mode=mode)
    print('mode', mode, 'ok', r.get('ok'), 'rc', r.get('returncode'), 'out', (r.get('stdout_tail') or '')[-200:])
    time.sleep(2.0)
    a1 = fb()
    print('a1 mode', mode, a1, 'd0', (a1[0]-a0[0]) if a1 else None)
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PW, timeout=20)
sftp = ssh.open_sftp()
with sftp.file(f"{BASE}/scripts/_diag_jog_modes.py", "w") as f:
    f.write(SCRIPT)
sftp.close()
_, o, _ = ssh.exec_command(
    f"cd {BASE} && . scripts/nx_d1_jog_env.sh && python3 scripts/_diag_jog_modes.py 2>/dev/null",
    timeout=60,
)
print(o.read().decode())
ssh.close()

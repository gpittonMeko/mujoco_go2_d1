#!/usr/bin/env python3
import paramiko
import time

HOST, USER, PW = "192.168.123.18", "unitree", "123"
BASE = "/home/unitree/go2_visual_dashboard"

SCRIPT = r"""
import os, sys, time
sys.path.insert(0, '/home/unitree/go2_visual_dashboard')
os.chdir('/home/unitree/go2_visual_dashboard')
os.environ['D1_JOG_ENABLE_REAL_ARM'] = '1'
os.environ['GO2_ENABLE_REAL_ARM'] = '1'
from go2_dashboard.d1_jog import service
fb = service.read_servo_deg(fast=True)
print('fb', fb.get('ok'), fb.get('servo_deg'))
service.enable_all()
time.sleep(0.5)
sd = list(fb['servo_deg'])
sd[0] += 8.0
r = service.jog_pose_deg(sd)
print('jog_ok', r.get('ok'), r.get('skipped'), r.get('reason'), 'rc', r.get('returncode'))
time.sleep(1.5)
fb2 = service.read_servo_deg(fast=True)
print('fb2', fb2.get('servo_deg'))
if fb.get('ok') and fb2.get('ok'):
    print('delta0', fb2['servo_deg'][0] - fb['servo_deg'][0])
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PW, timeout=20)
sftp = ssh.open_sftp()
with sftp.file(f"{BASE}/scripts/_diag_jog_once.py", "w") as f:
    f.write(SCRIPT)
sftp.close()

_, o, e = ssh.exec_command(
    f"cd {BASE} && . scripts/nx_d1_jog_env.sh && python3 scripts/_diag_jog_once.py",
    timeout=45,
)
print(o.read().decode())
print(e.read().decode())
ssh.close()

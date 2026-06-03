#!/usr/bin/env python3
import json
import paramiko
import time

HOST, USER, PW = "192.168.123.18", "unitree", "123"
BASE = "/home/unitree/go2_visual_dashboard"


def run(ssh, cmd, t=25):
    _, o, e = ssh.exec_command(cmd, timeout=t)
    o.channel.recv_exit_status()
    return (o.read() + e.read()).decode(errors="replace")


def angles(ssh, env):
    out = run(ssh, env + "bin/d1_sdk_feedback 0 2 2>&1 | grep '^servo_angles ' | tail -1")
    if "servo_angles" not in out:
        return None
    return [float(x) for x in out.strip().split()[1:8]]


ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PW, timeout=20)
env = f"cd {BASE} && . scripts/nx_d1_jog_env.sh && "

run(ssh, env + 'curl -s -X POST http://127.0.0.1:5053/api/joints/enable -H "Content-Type: application/json" -d \'{"mode":1}\'')
time.sleep(0.3)
a0 = angles(ssh, env)
print("before", a0)
if a0:
    sd = a0[:]
    sd[0] += 5.0
    body = json.dumps({"servo_deg": sd, "with_enable": False})
    run(
        ssh,
        env
        + f"""curl -s -X POST http://127.0.0.1:5053/api/joints/jog -H "Content-Type: application/json" -d '{body}'""",
    )
    time.sleep(1.5)
    a1 = angles(ssh, env)
    print("after", a1)
    if a1:
        print("delta j0", round(a1[0] - a0[0], 2))

ssh.close()

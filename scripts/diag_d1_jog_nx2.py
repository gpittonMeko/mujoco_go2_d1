#!/usr/bin/env python3
"""Confronta feedback reale vs stato stream durante jog."""
from __future__ import annotations

import json
import time

import paramiko

HOST, USER, PW = "192.168.123.18", "unitree", "123"
BASE = "/home/unitree/go2_visual_dashboard"


def run(ssh, cmd, timeout=25):
    _, o, e = ssh.exec_command(cmd, timeout=timeout)
    o.channel.recv_exit_status()
    return (o.read() + e.read()).decode(errors="replace")


def fb_angles(ssh, env):
    out = run(ssh, env + "bin/d1_sdk_feedback 0 2 2>&1 | grep '^servo_angles ' | tail -1")
    if "servo_angles" not in out:
        return None
    return [float(x) for x in out.strip().split()[1:8]]


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PW, timeout=20)
    env = f"cd {BASE} && . scripts/nx_d1_jog_env.sh && "

    print("enable funcode 5")
    run(ssh, env + 'printf \'{"seq":1,"address":1,"funcode":5,"data":{"mode":1}}\\n\' | bin/d1_sdk_command 0 80 2>&1 | tail -3')

    a0 = fb_angles(ssh, env)
    print("before", a0)

    body = json.dumps({"axis": "x", "sign": 1, "velocity_pct": 60, "max_speed_mm_s": 60})
    run(
        ssh,
        f"""python3 -c "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:5053/api/cartesian/jog_start', data={body!r}.encode(), headers={{'Content-Type':'application/json'}}, method='POST'), timeout=10)" """,
    )
    time.sleep(3)
    a1 = fb_angles(ssh, env)
    st = run(ssh, "curl -s http://127.0.0.1:5053/api/cartesian/jog_status")
    run(ssh, "curl -s -X POST http://127.0.0.1:5053/api/cartesian/jog_stop")
    print("after feedback", a1)
    print("status", st[:400])
    if a0 and a1:
        delta = [round(a1[i] - a0[i], 2) for i in range(7)]
        print("delta deg", delta, "moved:", any(abs(d) > 0.3 for d in delta))
    ssh.close()


if __name__ == "__main__":
    main()

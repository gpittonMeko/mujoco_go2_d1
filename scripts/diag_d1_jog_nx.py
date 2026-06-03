#!/usr/bin/env python3
"""Diagnostica jog D1 sulla NX."""
from __future__ import annotations

import json
import time

import paramiko

HOST = "192.168.123.18"
USER = "unitree"
PASSWORD = "123"
BASE = "/home/unitree/go2_visual_dashboard"


def run(ssh: paramiko.SSHClient, cmd: str, timeout: float = 30.0) -> tuple[int, str, str]:
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    code = stdout.channel.recv_exit_status()
    return code, stdout.read().decode(errors="replace"), stderr.read().decode(errors="replace")


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=25)
    env = f"cd {BASE} && . scripts/nx_d1_jog_env.sh && "

    print("=== feedback ===")
    code, out, err = run(ssh, env + "bin/d1_sdk_feedback 0 2 2>&1 | tail -8", 20)
    print(out or err)

    print("=== one-shot command (funcode 2 small delta) ===")
    # read angles first
    code, out, _ = run(ssh, env + "bin/d1_sdk_feedback 0 2 2>&1 | grep '^servo_angles ' | tail -1")
    print("angles line:", out.strip())
    if "servo_angles" not in out:
        print("NO FEEDBACK")
    else:
        parts = out.strip().split()[1:8]
        angles = [float(x) for x in parts]
        angles[0] += 2.0  # j0 +2 deg test
        data = {"mode": 0}
        for i, a in enumerate(angles[:7]):
            data[f"angle{i}"] = round(a, 3)
        msg = json.dumps({"seq": 99, "address": 1, "funcode": 2, "data": data}, separators=(",", ":"))
        cmd = env + f"printf '%s\\n' '{msg}' | bin/d1_sdk_command 0 20 2>&1 | tail -5"
        code, out, err = run(ssh, cmd, 25)
        print("cmd out:", out, err)

    print("=== jog_start API ===")
    code, out, _ = run(
        ssh,
        env
        + """python3 - <<'PY'
import json, urllib.request
body = {"axis": "x", "sign": 1, "velocity_pct": 50, "max_speed_mm_s": 60}
req = urllib.request.Request(
    "http://127.0.0.1:5053/api/cartesian/jog_start",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
print(urllib.request.urlopen(req, timeout=12).read().decode())
PY""",
        20,
    )
    print(out)

    time.sleep(2)
    print("=== jog_status ===")
    code, out, _ = run(ssh, "curl -s -m 6 http://127.0.0.1:5053/api/cartesian/jog_status")
    print(out)

    print("=== d1_sdk_command processes ===")
    code, out, _ = run(ssh, "pgrep -af 'd1_sdk_command' || echo none")
    print(out)

    print("=== jog log tail ===")
    code, out, _ = run(ssh, f"tail -30 {BASE}/d1_jog_run.log 2>/dev/null || echo no log")
    print(out)

    run(ssh, "curl -s -m 5 -X POST http://127.0.0.1:5053/api/cartesian/jog_stop")
    ssh.close()


if __name__ == "__main__":
    main()

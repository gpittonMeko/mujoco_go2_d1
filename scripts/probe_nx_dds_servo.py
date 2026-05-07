#!/usr/bin/env python3
"""SSH sulla NX e stampa traceback creazione ChannelSubscriber LowState vs PubServoInfo (debug DDS braccio)."""
from __future__ import annotations

import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from deploy_dashboard_to_nx import REMOTE_BASE, nx_host, nx_password, nx_user

REMOTE_PY = r"""
import os, traceback
from pathlib import Path
root = Path.cwd().resolve()
import sys
sys.path.insert(0, str(root / "unitree_sdk2_python"))
from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_
import cyclonedds.idl as idl
import cyclonedds.idl.annotations as annotate
import cyclonedds.idl.types as types
from dataclasses import dataclass

@dataclass
@annotate.final
@annotate.autoid("sequential")
class PubServoInfo_(idl.IdlStruct, typename="unitree_arm.msg.dds_.PubServoInfo_"):
    servo0_data: types.float32
    servo1_data: types.float32
    servo2_data: types.float32
    servo3_data: types.float32
    servo4_data: types.float32
    servo5_data: types.float32
    servo6_data: types.float32

domain = int(os.environ.get("GO2_DDS_DOMAIN", "0"))
iface = (os.environ.get("GO2_DDS_INTERFACE") or "").strip() or None
print("domain", domain, "iface", repr(iface))
try:
    if iface:
        ChannelFactoryInitialize(domain, iface)
    else:
        ChannelFactoryInitialize(domain)
    print("factory OK")
except Exception:
    traceback.print_exc()
    raise SystemExit(1)

try:
    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init()
    print("LowState rt/lowstate subscriber OK")
    sub.Close()
except Exception:
    print("LowState FAIL")
    traceback.print_exc()

for tn in ("current_servo_angle", "rt/current_servo_angle"):
    try:
        sub = ChannelSubscriber(tn, PubServoInfo_)
        sub.Init()
        print("PubServoInfo", tn, "subscriber OK")
        sub.Close()
    except Exception:
        print("PubServoInfo", tn, "FAIL")
        traceback.print_exc()
""".strip()


def main() -> int:
    host = nx_host()
    print(f"[probe_nx_dds_servo] SSH {nx_user()}@{host}\n")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, username=nx_user(), password=nx_password(), timeout=45)
    except Exception as exc:
        print("SSH failed:", exc)
        return 2
    stdin, stdout, stderr = ssh.exec_command("bash -s")
    stdin.write(
        f"set -e\ncd {REMOTE_BASE}\nsource scripts/nx_dashboard_env.sh\n"
        "python3 <<'PY'\n"
        + REMOTE_PY
        + "\nPY\n"
    )
    stdin.flush()
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    ssh.close()
    print(out)
    if err.strip():
        print("--- stderr ---")
        print(err)
    return int(code != 0)


if __name__ == "__main__":
    raise SystemExit(main())

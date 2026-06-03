#!/usr/bin/env python3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from deploy_d1_jog_to_nx import REMOTE_BASE, nx_host, nx_password, nx_user

import paramiko

from _run_rs_probe_on_nx import INLINE  # noqa: E402

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=25)
sftp = ssh.open_sftp()
with sftp.file(f"{REMOTE_BASE}/scripts/_rs_probe_tmp.py", "w") as f:
    f.write(INLINE.strip() + "\n")
sftp.close()

def run(cmd: str) -> None:
    _, o, _ = ssh.exec_command(cmd, timeout=120)
    print("===", cmd[:70], "===")
    print(o.read().decode(errors="replace"))

run("for i in 0 1 2 3 4 5; do echo -n video$i:; cat /sys/class/video4linux/video$i/name 2>/dev/null; done")
run(f"pkill -f serve_d1_jog_dashboard.py 2>/dev/null; sleep 2; cd {REMOTE_BASE} && . scripts/nx_d1_jog_env.sh && python3 scripts/_rs_probe_tmp.py")
run(f"cd {REMOTE_BASE} && bash scripts/nx_start_d1_jog.sh")
ssh.close()

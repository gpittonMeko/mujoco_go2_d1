#!/usr/bin/env python3
"""Diagnostica 500 su API POST dashboard jog NX."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.deploy_d1_jog_to_nx import nx_host, nx_password, nx_user

import paramiko

REMOTE = "/home/unitree/go2_visual_dashboard"
REMOTE_SCRIPT = f"{REMOTE}/scripts/_diag_couple_test.py"
LOCAL_SCRIPT = ROOT / "scripts" / "_diag_couple_test.py"

LOCAL_SCRIPT.write_text(
    """import traceback
from go2_dashboard.d1_jog.service import ensure_coupled, cartesian_end_jog
try:
    print("=== ensure_coupled ===")
    print(ensure_coupled(with_power=True, force=True))
    print("=== cartesian_end_jog ===")
    print(cartesian_end_jog(hold_after=False))
except Exception:
    traceback.print_exc()
""",
    encoding="utf-8",
)


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=25)
    sftp = ssh.open_sftp()
    sftp.put(str(LOCAL_SCRIPT), REMOTE_SCRIPT)
    sftp.close()
    cmd = f"cd {REMOTE} && . scripts/nx_d1_jog_env.sh && python3 scripts/_diag_couple_test.py"
    _, stdout, stderr = ssh.exec_command(cmd, timeout=60)
    print(stdout.read().decode(errors="replace"))
    err = stderr.read().decode(errors="replace")
    if err.strip():
        print("stderr:", err[-1500:])
    ssh.close()


if __name__ == "__main__":
    main()

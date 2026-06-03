#!/usr/bin/env python3
"""Sulla Jetson: copia sorgenti helper D1, compila ``bin/d1_arm_feedback_helper`` con env DDS dashboard, smoke test.

PC (rete verso NX): ``python scripts/nx_remote_build_d1_helpers.py``

Env: GO2_NX_HOST, GO2_NX_USER, GO2_NX_PASSWORD (come deploy_dashboard_to_nx).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
_D1_BUILD = _REPO / "D1 550 Workspace/OLD/scripts/build_d1_arm_helpers.sh"
REMOTE = "/home/unitree/go2_visual_dashboard"
REMOTE_BUILD = "scripts/build_d1_arm_helpers.sh"


def _nx_host() -> str:
    return (os.environ.get("GO2_NX_HOST") or "192.168.123.18").strip() or "192.168.123.18"


def _nx_user() -> str:
    return (os.environ.get("GO2_NX_USER") or "unitree").strip() or "unitree"


def _nx_password() -> str:
    return os.environ.get("GO2_NX_PASSWORD") or "123"


def main() -> int:
    try:
        import paramiko
    except ImportError:
        print("Install paramiko: pip install paramiko", file=sys.stderr)
        return 1

    if not _D1_BUILD.is_file():
        print("Missing local file:", _D1_BUILD, file=sys.stderr)
        return 1

    def put_lf(sftp: "paramiko.SFTPClient", local_path: Path, remote_path: str) -> None:
        data = local_path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        with sftp.open(remote_path, "wb") as rf:
            rf.write(data)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(_nx_host(), username=_nx_user(), password=_nx_password(), timeout=60)
    try:
        sftp = client.open_sftp()
        try:
            rp = f"{REMOTE}/{REMOTE_BUILD}"
            put_lf(sftp, _D1_BUILD, rp)
            print("pushed", _D1_BUILD, "->", REMOTE_BUILD)
        finally:
            sftp.close()

        chmod = f"chmod a+x {REMOTE}/scripts/build_d1_arm_helpers.sh"
        client.exec_command(chmod)

        remote_bash = rf"""set -eo pipefail
cd {REMOTE}
if [ -d .git ]; then
  git checkout -- scripts/d1_arm_dds_helper.cpp scripts/d1_arm_feedback_helper.cpp 2>/dev/null || true
fi
set -a
[ -f scripts/nx_dashboard_env.sh ] && . scripts/nx_dashboard_env.sh
set +a
export UNITREE_SDK2="${{UNITREE_SDK2:-/usr/local}}"
export UNITREE_INCLUDE="${{UNITREE_INCLUDE:-$UNITREE_SDK2/include}}"
export UNITREE_LIB="${{UNITREE_LIB:-$UNITREE_SDK2/lib}}"
export ICEORYX_CPP_INC="${{ICEORYX_CPP_INC:-/usr/local/include/iceoryx/v2.0.2}}"
echo "=== LD_LIBRARY_PATH (head) ==="
echo "$LD_LIBRARY_PATH" | tr ':' '\n' | head -8
echo "=== build ==="
bash scripts/build_d1_arm_helpers.sh
echo "=== ldd feedback helper (head) ==="
ldd bin/d1_arm_feedback_helper 2>&1 | head -25
echo "=== run helper 2s domain 0 ==="
./bin/d1_arm_feedback_helper 0 2 2>&1 | tail -15
"""
        stdin, stdout, stderr = client.exec_command(remote_bash)
        # Channel timeout: wait up to 240s
        ch = stdout.channel
        t0 = time.monotonic()
        while not ch.exit_status_ready():
            if time.monotonic() - t0 > 240:
                ch.close()
                print("REMOTE_TIMEOUT", file=sys.stderr)
                return 2
            time.sleep(0.25)
        out_b = stdout.read()
        err_b = stderr.read()
        code = ch.recv_exit_status()
        if out_b:
            print(out_b.decode(errors="replace"))
        if err_b:
            print(err_b.decode(errors="replace"), file=sys.stderr)
        print("remote_exit=", code)
        return 0 if code == 0 else code
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Deploy Vision Workspace dashboard sulla Jetson (porta 5054)."""
from __future__ import annotations

import os
from pathlib import Path

import paramiko

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_BASE = "/home/unitree/go2_visual_dashboard"

PUSH_FILES = [
    "go2_dashboard/__init__.py",
    "go2_dashboard/paths.py",
    "go2_dashboard/cameras.py",
    "go2_dashboard/vision/__init__.py",
    "go2_dashboard/vision/app.py",
    "templates/vision_dashboard.html",
    "scripts/serve_vision_dashboard.py",
    "scripts/nx_vision_env.sh",
    "scripts/nx_start_vision.sh",
    "Vision Workspace/README.md",
]


def nx_host() -> str:
    return (os.environ.get("GO2_NX_HOST") or "192.168.123.18").strip() or "192.168.123.18"


def nx_user() -> str:
    return (os.environ.get("GO2_NX_USER") or "unitree").strip() or "unitree"


def nx_password() -> str:
    return os.environ.get("GO2_NX_PASSWORD") or "123"


def main() -> None:
    host = nx_host()
    print(f"[vision deploy] {nx_user()}@{host}")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=nx_user(), password=nx_password(), timeout=25)
    sftp = ssh.open_sftp()
    for rel in PUSH_FILES:
        loc = REPO_ROOT / rel
        if not loc.is_file():
            print("skip", rel)
            continue
        remote = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        parent = "/".join(remote.split("/")[:-1])
        try:
            sftp.stat(parent)
        except OSError:
            parts = parent.split("/")
            cur = ""
            for p in parts:
                if not p:
                    continue
                cur = f"{cur}/{p}"
                try:
                    sftp.stat(cur)
                except OSError:
                    sftp.mkdir(cur)
        sftp.put(str(loc), remote)
        print("pushed", rel)
    sftp.close()
    for sh in ("scripts/nx_vision_env.sh", "scripts/nx_start_vision.sh"):
        ssh.exec_command(f"chmod +x {REMOTE_BASE}/{sh}")
    _, o, _ = ssh.exec_command(f"bash {REMOTE_BASE}/scripts/nx_start_vision.sh", timeout=60)
    print(o.read().decode(errors="replace"))
    ssh.close()
    print(f"[vision deploy] OK — http://{host}:5054/")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Ripara CRLF sugli .sh NX, riavvia serve_dashboard_lite :5052, verifica grasp/health."""
from __future__ import annotations

import json
import os
import sys
import urllib.request

import paramiko

REMOTE_BASE = "/home/unitree/go2_visual_dashboard"


def main() -> int:
    host = (os.environ.get("GO2_NX_HOST") or "192.168.123.18").strip()
    user = (os.environ.get("GO2_NX_USER") or "unitree").strip()
    pw = os.environ.get("GO2_NX_PASSWORD") or "123"
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=pw, timeout=45)
    cmds = [
        f"find {REMOTE_BASE}/scripts -maxdepth 1 -name '*.sh' -exec sed -i 's/\\r$//' {{}} +",
        "pkill -f serve_dashboard_modular 2>/dev/null || true",
        "pkill -f nx_dashboard_supervise 2>/dev/null || true",
        "pkill -f serve_dashboard_lite 2>/dev/null || true",
        "sleep 2",
        f"cd {REMOTE_BASE} && bash scripts/nx_start_dashboard.sh",
    ]
    for c in cmds:
        _, o, e = ssh.exec_command(c, timeout=180)
        out = o.read().decode()
        err = e.read().decode()
        if out.strip():
            print(out[-3000:])
        if err.strip():
            print("stderr:", err[-600:], file=sys.stderr)
    verify = (
        f"bash -lc 'source {REMOTE_BASE}/scripts/nx_dashboard_env.sh; "
        f"[[ -f {REMOTE_BASE}/scripts/nx_secrets_dashboard.sh ]] && source {REMOTE_BASE}/scripts/nx_secrets_dashboard.sh; "
        "curl -sf http://127.0.0.1:5052/api/grasp/health'"
    )
    _, o, e = ssh.exec_command(verify, timeout=30)
    raw = o.read().decode().strip()
    if not raw:
        print("FAIL grasp health:", e.read().decode(), file=sys.stderr)
        ssh.close()
        return 1
    print("--- NX /api/grasp/health ---")
    print(json.dumps(json.loads(raw), indent=2, ensure_ascii=False)[:4000])
    ssh.close()
    try:
        urllib.request.urlopen(f"http://{host}:5052/api/grasp/health", timeout=10)
        print(f"OK LAN http://{host}:5052/api/grasp/health")
    except OSError as exc:
        print(f"LAN health warn: {exc}")
    print("NX_FIX_RESTART_LITE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

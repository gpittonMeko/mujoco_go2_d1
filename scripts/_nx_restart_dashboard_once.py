#!/usr/bin/env python3
"""Riavvio dashboard sulla NX: strip CRLF, env, build helper legacy, avvio lite."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import deploy_dashboard_to_nx as d  # noqa: E402


def _run(ssh, cmd: str) -> tuple[int, str, str]:
    stdin, stdout, stderr = ssh.exec_command(cmd)
    code = stdout.channel.recv_exit_status()
    return code, stdout.read().decode(errors="replace"), stderr.read().decode(errors="replace")


def _http_json(url: str, timeout: float = 10.0) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "url": url}


def main() -> int:
    host = d.nx_host()
    print(f"[restart] {d.nx_user()}@{host}")
    import paramiko

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=d.nx_user(), password=d.nx_password(), timeout=60)
    base = d.REMOTE_BASE
    sftp = ssh.open_sftp()
    for path, content, mode in (
        (f"{base}/scripts/nx_dashboard_env.sh", d._nx_dashboard_env_sh(), 0o644),
        (f"{base}/scripts/nx_start_dashboard.sh", d._nx_start_dashboard_sh(host), 0o755),
        (
            f"{base}/scripts/nx_dashboard_supervise.sh",
            (REPO / "scripts/nx_dashboard_supervise.sh").read_text(encoding="utf-8"),
            0o755,
        ),
    ):
        with sftp.file(path, "wb") as rf:
            rf.write(content.replace("\r\n", "\n").encode("utf-8"))
        sftp.chmod(path, mode)
        print("wrote", path)
    sftp.close()

    steps = [
        f"cd {base} && find scripts -name '*.sh' -exec sed -i 's/\\r$//' {{}} \\;",
        f"cd {base} && bash scripts/build_d1_arm_helpers.sh",
        f"cd {base} && ls -la bin/d1_sdk_command bin/d1_sdk_feedback bin/d1_arm_command bin/d1_arm_feedback_helper 2>/dev/null || true",
        "pkill -f nx_dashboard_supervise || true; pkill -f serve_dashboard || true; pkill -f diagnostics_dashboard || true; sleep 2",
        (
            f"cd {base} && set -a && . scripts/nx_dashboard_env.sh && set +a && "
            f"export GO2_DASHBOARD_SERVE=lite && "
            f"nohup python3 scripts/serve_dashboard_lite.py >> dashboard_run.log 2>&1 &"
        ),
        "sleep 10",
        "curl -s -m 10 http://127.0.0.1:${GO2_DASHBOARD_PORT:-5052}/api/health",
        "curl -s -m 15 \"http://127.0.0.1:${GO2_DASHBOARD_PORT:-5052}/api/arm/servo_snapshot?diag=1\" | head -c 2000",
        f"tail -30 {base}/dashboard_run.log",
    ]
    for cmd in steps:
        code, out, err = _run(ssh, cmd)
        label = cmd[:70].replace("\n", " ")
        print(f"\n--- {label}... exit={code} ---")
        if out.strip():
            print(out.strip()[-3000:])
        if err.strip():
            print("stderr:", err.strip()[-800:])
    ssh.close()

    time.sleep(2)
    base_url = f"http://{host}:5052"
    for path in ("/api/health", "/api/arm/servo_snapshot?diag=1"):
        data = _http_json(base_url + path, timeout=20.0)
        print(f"\n[verify PC] {path}:")
        print(json.dumps(data, indent=2)[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

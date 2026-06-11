#!/usr/bin/env python3
"""Deploy allineamento operator + braccio SDK (veloce, no mesh)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import deploy_dashboard_to_nx as d  # noqa: E402

# File critici operator + braccio (oltre a REMOTE_PUSH già in deploy pieno)
EXTRA_REL = [
    "go2_dashboard/paths.py",
    "go2_dashboard/d1_servo_feedback.py",
    "go2_dashboard/d1_arm_publish_lite.py",
    "go2_dashboard/d1_arm_motion.py",
    "go2_dashboard/blueprints/operator_api/routes.py",
    "templates/dashboard_operators.html",
    "static/js/operators_arm_joints.js",
    "scripts/_nx_restart_dashboard_once.py",
]


def _connect():
    import paramiko

    host = d.nx_host()
    for attempt in range(1, 6):
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                host,
                username=d.nx_user(),
                password=d.nx_password(),
                timeout=90,
                banner_timeout=90,
                auth_timeout=90,
            )
            print(f"[align] SSH OK {d.nx_user()}@{host} (tentativo {attempt})")
            return ssh
        except Exception as exc:
            print(f"[align] SSH tentativo {attempt} fallito: {exc}")
            time.sleep(3)
    raise SystemExit(1)


def main() -> int:
    os.environ["GO2_DEPLOY_SKIP_MESHES"] = "1"
    ssh = _connect()
    base = d.REMOTE_BASE
    ssh.exec_command(
        f'mkdir -p "{base}/go2_dashboard/d1_jog" "{base}/D1 550 Workspace/d1_sdk" '
        f"{base}/templates {base}/static/js {base}/bin"
    )[1].channel.recv_exit_status()

    sftp = ssh.open_sftp()
    pushed: set[str] = set()
    for rel in list(d.REMOTE_PUSH_FILES) + EXTRA_REL + d._d1_jog_rel_paths() + d._d1_sdk_rel_paths():
        if rel in pushed:
            continue
        loc = d.REPO_ROOT / rel
        if not loc.is_file():
            continue
        remote = f"{base}/{rel.replace(chr(92), '/')}"
        sftp.put(str(loc), remote)
        pushed.add(rel)
        print("pushed", rel)

    for path, content, mode in (
        (f"{base}/scripts/nx_dashboard_env.sh", d._nx_dashboard_env_sh(), 0o644),
        (f"{base}/scripts/nx_start_dashboard.sh", d._nx_start_dashboard_sh(d.nx_host()), 0o755),
        (
            f"{base}/scripts/nx_dashboard_supervise.sh",
            (REPO / "scripts/nx_dashboard_supervise.sh").read_text(encoding="utf-8").replace("\r\n", "\n"),
            0o755,
        ),
    ):
        with sftp.file(path, "wb") as rf:
            rf.write(content.encode("utf-8"))
        sftp.chmod(path, mode)
        print("wrote", path)
    sftp.close()

    ssh.exec_command(f"bash -lc 'cd {base} && find scripts -name \"*.sh\" -exec sed -i \"s/\\r$//\" {{}} \\;'")[1].channel.recv_exit_status()
    d._remote_build_d1_sdk(ssh)
    d._remote_build_d1_arm_helpers(ssh)

    print("[align] Riavvio dashboard …")
    stdin, stdout, stderr = ssh.exec_command(f"bash {base}/scripts/nx_start_dashboard.sh")
    print(stdout.read().decode(errors="replace"))
    err = stderr.read().decode(errors="replace")
    if err.strip():
        print("stderr:", err[-2000:])

    host = d.nx_host()
    port = "5052"
    verify = (
        f"sleep 5 && curl -sf -m 12 http://127.0.0.1:{port}/api/health && "
        f"curl -sf -m 15 'http://127.0.0.1:{port}/api/arm/servo_snapshot?diag=1' | head -c 800"
    )
    _, o, _ = ssh.exec_command(verify)
    o.channel.recv_exit_status()
    print("[align] verify NX:\n", o.read().decode(errors="replace"))
    ssh.close()

    import json
    import urllib.request

    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            h = json.loads(resp.read())
        print(f"[align] verify PC {url}:", json.dumps(h, indent=2))
        if h.get("d1_arm_motion_backend") != "d1_sdk":
            print("[align] WARN: backend non d1_sdk — controlla bin sulla NX")
    except Exception as exc:
        print(f"[align] verify PC fallito: {exc}")
        return 1
    print("[align] DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

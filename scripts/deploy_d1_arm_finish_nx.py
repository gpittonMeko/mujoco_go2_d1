#!/usr/bin/env python3
"""Completa deploy braccio se il deploy pieno si interrompe sui mesh: d1_jog, SDK, build, restart."""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import deploy_dashboard_to_nx as d  # noqa: E402


def main() -> int:
    os.environ.setdefault("GO2_DEPLOY_SKIP_MESHES", "1")
    host = d.nx_host()
    print(f"[finish] SSH {d.nx_user()}@{host} — solo fase braccio + restart …")
    import paramiko

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=d.nx_user(), password=d.nx_password(), timeout=60)
    ssh.exec_command(
        f'mkdir -p "{d.REMOTE_BASE}/go2_dashboard/d1_jog" "{d.REMOTE_BASE}/D1 550 Workspace/d1_sdk" {d.REMOTE_BASE}/bin'
    )[1].channel.recv_exit_status()
    sftp = ssh.open_sftp()
    for rel in d._d1_jog_rel_paths() + d._d1_sdk_rel_paths():
        loc = d.REPO_ROOT / rel
        if not loc.is_file():
            continue
        remote_path = f"{d.REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        sftp.put(str(loc), remote_path)
        print("pushed", rel)
    for path, content, mode in (
        (f"{d.REMOTE_BASE}/scripts/nx_dashboard_env.sh", d._nx_dashboard_env_sh(), 0o644),
        (f"{d.REMOTE_BASE}/scripts/nx_start_dashboard.sh", d._nx_start_dashboard_sh(host), 0o755),
    ):
        with sftp.file(path, "wb") as rf:
            rf.write(content.encode("utf-8"))
        sftp.chmod(path, mode)
        print("wrote", path)
    sftp.close()
    d._remote_build_d1_sdk(ssh)
    d._remote_build_d1_arm_helpers(ssh)
    print("[finish] Riavvio dashboard …")
    stdin, stdout, stderr = ssh.exec_command(f"bash {d.REMOTE_BASE}/scripts/nx_start_dashboard.sh")
    print(stdout.read().decode(errors="replace"))
    err = stderr.read().decode(errors="replace")
    if err.strip():
        print("stderr:", err)
    ssh.close()
    print("[finish] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

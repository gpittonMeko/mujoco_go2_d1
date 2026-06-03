#!/usr/bin/env python3
"""Deploy dashboard jog D1 sulla Jetson — **separata** dalla dashboard operator (5052).

Non modifica nx_dashboard_env.sh, nx_start_dashboard.sh né riavvia nx_dashboard_supervise.

Uso (PC sulla LAN Unitree):
  python scripts/deploy_d1_jog_to_nx.py

Env: GO2_NX_HOST, GO2_NX_USER, GO2_NX_PASSWORD (come deploy_dashboard_to_nx.py).

URL: http://192.168.123.18:5053/  (Vision: /vision sulla stessa porta)
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import paramiko

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_BASE = "/home/unitree/go2_visual_dashboard"
SDK_TREE = REPO_ROOT / "D1 550 Workspace" / "d1_sdk" / "d1_sdk"

PUSH_FILES = [
    "go2_dashboard/__init__.py",
    "go2_dashboard/paths.py",
    "go2_dashboard/d1_jog/__init__.py",
    "go2_dashboard/d1_jog/app.py",
    "go2_dashboard/d1_jog/service.py",
    "go2_dashboard/d1_jog/motion_guard.py",
    "go2_dashboard/d1_jog/motion_profile.py",
    "go2_dashboard/d1_jog/program_store.py",
    "go2_dashboard/d1_jog/program_runner.py",
    "go2_dashboard/d1_jog/tcp_motion.py",
    "go2_dashboard/d1_jog/jog_stream.py",
    "go2_dashboard/d1_jog/cartesian.py",
    "go2_dashboard/d1_jog/vision_page.py",
    "go2_dashboard/d1_jog/vision_detect.py",
    "go2_dashboard/d1_jog/vision_fusion.py",
    "go2_dashboard/d1_jog/vision_streams.py",
    "go2_dashboard/d1_jog/vision_yolo.py",
    "go2_dashboard/cameras.py",
    "go2_dashboard/realsense_pyrs.py",
    "scripts/box_object_detector.py",
    "D1 550 Workspace/OLD/scripts/arm_kinematics_d1_template.py",
    "scripts/serve_d1_jog_dashboard.py",
    "scripts/build_d1_sdk.sh",
    "scripts/nx_d1_jog_env.sh",
    "scripts/nx_d1_jog_supervise.sh",
    "scripts/nx_start_d1_jog.sh",
    "scripts/nx_install_yolo_vision.sh",
    "templates/d1_jog_dashboard.html",
    "templates/d1_program_editor.html",
    "templates/vision_dashboard.html",
    "templates/_d1_dash_switch.html",
    "templates/d1_tcp_jog_modal.html",
    "static/d1_common.js",
    "static/d1_common.css",
]


def nx_host() -> str:
    return (os.environ.get("GO2_NX_HOST") or "192.168.123.18").strip() or "192.168.123.18"


def nx_user() -> str:
    return (os.environ.get("GO2_NX_USER") or "unitree").strip() or "unitree"


def nx_password() -> str:
    return os.environ.get("GO2_NX_PASSWORD") or "123"


def _sdk_files() -> list[Path]:
    if not SDK_TREE.is_dir():
        return []
    out: list[Path] = []
    for p in SDK_TREE.rglob("*"):
        if p.is_file():
            out.append(p)
    return out


def _ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    if not remote_dir or remote_dir == "/":
        return
    parts = [p for p in remote_dir.split("/") if p]
    cur = "/" if remote_dir.startswith("/") else ""
    for part in parts:
        cur = f"{cur}/{part}" if cur else part
        try:
            sftp.stat(cur)
        except OSError:
            try:
                sftp.mkdir(cur)
            except OSError:
                pass


def _put_file(sftp: paramiko.SFTPClient, local: Path, remote: str) -> None:
    parent = remote.rsplit("/", 1)[0]
    if parent:
        _ensure_remote_dir(sftp, parent)
    sftp.put(str(local), remote)


def _remote_build_d1_sdk(ssh: paramiko.SSHClient) -> int:
    print("[d1-jog deploy] Compilazione bin/d1_sdk_* …")
    log = "/tmp/go2_d1_sdk_build.log"
    script = f"""set +e
cd "{REMOTE_BASE}"
mkdir -p bin
set -a
if [ -f scripts/nx_d1_jog_env.sh ]; then
  . scripts/nx_d1_jog_env.sh
elif [ -f scripts/nx_dashboard_env.sh ]; then
  . scripts/nx_dashboard_env.sh
fi
set +a
bash scripts/build_d1_sdk.sh >{log} 2>&1
EC=$?
echo EXIT_CODE=$EC >>{log}
cat {log}
ls -la bin/d1_sdk_command bin/d1_sdk_feedback bin/d1_sdk_get_angles 2>/dev/null || true
exit $EC
"""
    _, stdout, stderr = ssh.exec_command(script, timeout=300)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if out.strip():
        print(out.strip()[-6000:])
    if err.strip():
        print("build stderr:", err.strip()[-800:])
    if code == 0:
        print("[d1-jog deploy] build_d1_sdk OK")
    else:
        print(f"[d1-jog deploy] ERRORE build_d1_sdk exit={code}")
    return code


def _remote_install_yolo(ssh: paramiko.SSHClient) -> None:
    print("[d1-jog deploy] YOLO (ultralytics + yolo11n.pt) …")
    _, stdout, stderr = ssh.exec_command(
        f"bash {REMOTE_BASE}/scripts/nx_install_yolo_vision.sh",
        timeout=600,
    )
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    if out.strip():
        print(out.strip()[-2000:].encode("ascii", errors="replace").decode("ascii"))
    if code != 0:
        if err.strip():
            print(err.strip()[-800:])
        print("[d1-jog deploy] WARN YOLO install — controlla rete pip / wget sulla NX")
    else:
        print("[d1-jog deploy] YOLO OK")


def _remote_install_pyrealsense(ssh: paramiko.SSHClient) -> None:
    print("[d1-jog deploy] pyrealsense2 (RGB RealSense) …")
    _, stdout, stderr = ssh.exec_command(
        "pip3 show pyrealsense2 >/dev/null 2>&1 || pip3 install -q pyrealsense2",
        timeout=180,
    )
    code = stdout.channel.recv_exit_status()
    if code != 0:
        print(stderr.read().decode(errors="replace")[-500:])
        print("[d1-jog deploy] WARN pyrealsense2 install failed — fallback V4L video4")
    else:
        print("[d1-jog deploy] pyrealsense2 OK")


def _remote_start_jog(ssh: paramiko.SSHClient, host: str) -> None:
    print("[d1-jog deploy] Avvio dashboard jog (solo 5053) …")
    _, stdout, stderr = ssh.exec_command(f"bash {REMOTE_BASE}/scripts/nx_start_d1_jog.sh", timeout=60)
    code = stdout.channel.recv_exit_status()
    print(stdout.read().decode(errors="replace"))
    err = stderr.read().decode(errors="replace")
    if err.strip():
        print("start stderr:", err.strip())
    if code != 0:
        raise SystemExit(code)
    port = os.environ.get("D1_JOG_PORT", "5053")
    print(f"[d1-jog deploy] OK — apri http://{host}:{port}/")


def main() -> None:
    host = nx_host()
    print(f"[d1-jog deploy] NX {nx_user()}@{host} (non tocca dashboard 5052)")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=nx_user(), password=nx_password(), timeout=25)

    sftp = ssh.open_sftp()
    try:
        sftp.stat(REMOTE_BASE)
    except OSError:
        print(f"ERROR: {REMOTE_BASE} non esiste — esegui prima deploy_dashboard_to_nx.py")
        sftp.close()
        ssh.close()
        raise SystemExit(2)

    for rel in PUSH_FILES:
        loc = REPO_ROOT / rel
        if not loc.is_file():
            print("skip missing", rel)
            continue
        remote = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        _put_file(sftp, loc, remote)
        print("pushed", rel)

    remote_sdk_root = f"{REMOTE_BASE}/D1 550 Workspace/d1_sdk/d1_sdk"
    for loc in _sdk_files():
        rel = loc.relative_to(SDK_TREE)
        remote = f"{remote_sdk_root}/{rel.as_posix()}"
        _put_file(sftp, loc, remote)
    print(f"pushed d1_sdk tree ({len(_sdk_files())} files)")

    for sh in (
        "scripts/nx_d1_jog_env.sh",
        "scripts/nx_d1_jog_supervise.sh",
        "scripts/nx_start_d1_jog.sh",
        "scripts/nx_install_yolo_vision.sh",
        "scripts/build_d1_sdk.sh",
    ):
        try:
            sftp.chmod(f"{REMOTE_BASE}/{sh}", 0o755)
        except OSError:
            pass

    strip_cmd = (
        f"bash -lc \"sed -i 's/\\\\r$//' {REMOTE_BASE}/scripts/nx_d1_jog*.sh "
        f"{REMOTE_BASE}/scripts/nx_start_d1_jog.sh {REMOTE_BASE}/scripts/build_d1_sdk.sh 2>/dev/null || true\""
    )
    _, so, _ = ssh.exec_command(strip_cmd)
    so.channel.recv_exit_status()
    sftp.close()

    build_code = _remote_build_d1_sdk(ssh)
    if build_code != 0:
        print("[d1-jog deploy] Avvio comunque Flask (health segnalerà bin mancanti)")
    _remote_install_pyrealsense(ssh)
    _remote_install_yolo(ssh)
    _remote_start_jog(ssh, host)
    ssh.close()


if __name__ == "__main__":
    main()

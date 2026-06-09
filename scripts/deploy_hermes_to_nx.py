#!/usr/bin/env python3
"""Deploy Hermes sulla Jetson (porta 5054).

Uso dal PC sulla LAN Unitree:
  py -3 scripts/deploy_hermes_to_nx.py

Apri dal PC: http://192.168.123.18:5054/
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import paramiko

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_BASE = "/home/unitree/go2_visual_dashboard"

PUSH_FILES = [
    "go2_dashboard/__init__.py",
    "go2_dashboard/hermes/__init__.py",
    "go2_dashboard/hermes/app.py",
    "go2_dashboard/hermes/routes.py",
    "go2_dashboard/hermes/agent.py",
    "go2_dashboard/hermes/local_agent.py",
    "go2_dashboard/hermes/context.py",
    "go2_dashboard/hermes/speech.py",
    "go2_dashboard/hermes/sdk_bridge.py",
    "go2_dashboard/hermes/vision.py",
    "go2_dashboard/hermes/actions.py",
    "go2_dashboard/hermes/phrases.py",
    "go2_dashboard/hermes/tts_local.py",
    "go2_dashboard/hermes/interaction.py",
    "go2_dashboard/hermes/locomotion.py",
    "scripts/build_hermes_canned_audio.py",
    "scripts/install_hermes_piper_model.py",
    "scripts/install_hermes_piper_binary.py",
    "templates/hermes.html",
    "static/hermes/hermes.css",
    "static/hermes/hermes.js",
    "scripts/serve_hermes_dashboard.py",
    "scripts/pc_go2_webrtc_speak.py",
    "scripts/go2_accompany.py",
    "scripts/sport_accompany_once.py",
    "scripts/nx_hermes_env.sh",
    "scripts/nx_hermes_supervise.sh",
    "scripts/nx_start_hermes.sh",
    "scripts/requirements-hermes.txt",
]


def nx_host() -> str:
    return (os.environ.get("GO2_NX_HOST") or "192.168.123.18").strip() or "192.168.123.18"


def nx_user() -> str:
    return (os.environ.get("GO2_NX_USER") or "unitree").strip() or "unitree"


def nx_password() -> str:
    return os.environ.get("GO2_NX_PASSWORD") or "123"


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


def _remote_pip_deps(ssh: paramiko.SSHClient) -> None:
    cmd = (
        "python3 -m pip install -q -r scripts/requirements-hermes.txt 2>/dev/null || "
        "python3 -m pip install -q flask gTTS pydub piper-tts onnxruntime 2>/dev/null; "
        "python3 -m pip show flask gTTS piper-tts onnxruntime pydub 2>/dev/null | grep -E '^Name:|^Version:'"
    )
    _, stdout, _ = ssh.exec_command(cmd, timeout=180)
    out = stdout.read().decode(errors="replace")
    if out.strip():
        print(out.strip())
    stdout.channel.recv_exit_status()


def _remote_start(ssh: paramiko.SSHClient, host: str) -> None:
    _, stdout, stderr = ssh.exec_command(f"bash {REMOTE_BASE}/scripts/nx_start_hermes.sh", timeout=60)
    print(stdout.read().decode(errors="replace"))
    err = stderr.read().decode(errors="replace")
    if err.strip():
        print("stderr:", err.strip())
    if stdout.channel.recv_exit_status() != 0:
        raise SystemExit(1)
    port = os.environ.get("HERMES_PORT", "5054")
    print(f"[hermes deploy] OK — http://{host}:{port}/")


def _build_canned_local() -> None:
    script = REPO_ROOT / "scripts" / "build_hermes_canned_audio.py"
    if not script.is_file():
        return
    print("[hermes deploy] build canned WAV…")
    subprocess.run([sys.executable, str(script)], cwd=str(REPO_ROOT), check=False)


def main() -> None:
    _build_canned_local()
    host = nx_host()
    print(f"[hermes deploy] NX {nx_user()}@{host}")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=nx_user(), password=nx_password(), timeout=25)

    sftp = ssh.open_sftp()
    try:
        sftp.stat(REMOTE_BASE)
    except OSError:
        print(f"ERROR: {REMOTE_BASE} missing — run deploy_dashboard_to_nx.py first")
        raise SystemExit(2)

    for rel in PUSH_FILES:
        loc = REPO_ROOT / rel
        if not loc.is_file():
            print("skip missing", rel)
            continue
        remote = f"{REMOTE_BASE}/{rel.replace(chr(92), '/')}"
        _put_file(sftp, loc, remote)
        print("pushed", rel)

    canned_dir = REPO_ROOT / "go2_dashboard" / "hermes" / "canned"
    if canned_dir.is_dir():
        for wav in sorted(canned_dir.glob("*.wav")):
            rel = f"go2_dashboard/hermes/canned/{wav.name}"
            remote = f"{REMOTE_BASE}/{rel}"
            _put_file(sftp, wav, remote)
            print("pushed", rel)
    else:
        print("warn: no canned/ — run scripts/build_hermes_canned_audio.py")

    for sh in ("scripts/nx_hermes_env.sh", "scripts/nx_hermes_supervise.sh", "scripts/nx_start_hermes.sh"):
        try:
            sftp.chmod(f"{REMOTE_BASE}/{sh}", 0o755)
        except OSError:
            pass

    strip = (
        f"bash -lc \"sed -i 's/\\r$//' {REMOTE_BASE}/scripts/nx_hermes*.sh "
        f"{REMOTE_BASE}/scripts/nx_start_hermes.sh 2>/dev/null || true\""
    )
    _, so, _ = ssh.exec_command(strip)
    so.channel.recv_exit_status()
    sftp.close()

    _remote_pip_deps(ssh)
    print("[hermes deploy] espeak (TTS veloce)…")
    ssh.exec_command(
        "command -v espeak-ng >/dev/null 2>&1 || "
        f"(echo '{nx_password()}' | sudo -S apt-get install -y espeak-ng ffmpeg 2>/dev/null) || true; "
        "command -v espeak-ng || command -v espeak || echo ESPEAK_MISSING",
        timeout=120,
    )
    print("[hermes deploy] Piper binary arm64…")
    _, so, _ = ssh.exec_command(
        f"cd {REMOTE_BASE} && python3 scripts/install_hermes_piper_binary.py 2>&1",
        timeout=300,
    )
    out = so.read().decode(errors="replace")
    if out.strip():
        print(out.strip()[-400:])
    so.channel.recv_exit_status()

    print("[hermes deploy] Piper model (voce IT)…")
    _, so, _ = ssh.exec_command(
        f"cd {REMOTE_BASE} && source scripts/nx_hermes_env.sh && python3 scripts/install_hermes_piper_model.py 2>&1",
        timeout=300,
    )
    piper_out = so.read().decode(errors="replace")
    if piper_out.strip():
        print(piper_out.strip()[-600:])
    so.channel.recv_exit_status()

    print("[hermes deploy] build canned WAV (stesso TTS di runtime)…")
    _, so, se = ssh.exec_command(
        f"cd {REMOTE_BASE} && source scripts/nx_hermes_env.sh && python3 scripts/build_hermes_canned_audio.py 2>&1",
        timeout=300,
    )
    build_out = so.read().decode(errors="replace")
    if build_out.strip():
        print(build_out.strip()[-800:])
    so.channel.recv_exit_status()

    _remote_start(ssh, host)
    ssh.close()


if __name__ == "__main__":
    main()

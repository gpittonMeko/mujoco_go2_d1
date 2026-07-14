#!/usr/bin/env python3
"""Deploy H2 demo scripts + unitree_sdk2_python to Jetson Thor (.163)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

REPO = Path(__file__).resolve().parent.parent
REMOTE = "/home/unitree/h2_demo"
HOST = os.environ.get("H2_JETSON_HOST", "192.168.123.163")
USER = os.environ.get("H2_JETSON_USER", "unitree")
PW = os.environ.get("H2_JETSON_PASSWORD", "123")

SCRIPTS = [
    "h2_common.py",
    "h2_wav_util.py",
    "h2_probe_lowstate.py",
    "h2_verify_sdk.py",
    "h2_hand_util.py",
    "h2_probe_hand.py",
    "h2_probe_hand_dds.py",
    "h2_scan_hand_topics.py",
    "h2_right_arm_hand.py",
    "h2_recover_stand.py",
    "h2_test_tts.py",
    "h2_left_arm_raise.py",
    "h2_play_wav.py",
    "h2_demo_pellegrino.py",
    "h2_demo_casoria.py",
    "h2_micro_right_arm.py",
    "h2_arm_nudge.py",
    "h2_arm_wave_test.py",
    "h2_arm_diag.py",
    "h2_verify_arm_sdk.py",
    "h2_arm_emergency_stop.py",
]


def _sftp_mkdir(sftp, path: str) -> None:
    parts = path.strip("/").split("/")
    cur = ""
    for p in parts:
        cur += "/" + p
        try:
            sftp.stat(cur)
        except OSError:
            sftp.mkdir(cur)


def _upload_dir(sftp, local: Path, remote: str) -> None:
    for root, dirs, files in os.walk(local):
        rel = Path(root).relative_to(local)
        rdir = remote if str(rel) == "." else f"{remote}/{rel.as_posix()}"
        _sftp_mkdir(sftp, rdir)
        for d in dirs:
            _sftp_mkdir(sftp, f"{rdir}/{d}")
        for f in files:
            if f.endswith(".pyc") or "__pycache__" in root:
                continue
            lp = Path(root) / f
            rp = f"{rdir}/{f}"
            sftp.put(str(lp), rp)
            print(f"  put {lp.relative_to(REPO)} -> {rp}")


def _read_unix_lf(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def main() -> int:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting {USER}@{HOST}...")
    c.connect(HOST, username=USER, password=PW, timeout=20, allow_agent=False, look_for_keys=False)

    sftp = c.open_sftp()
    _sftp_mkdir(sftp, REMOTE)
    _sftp_mkdir(sftp, f"{REMOTE}/scripts")
    _sftp_mkdir(sftp, f"{REMOTE}/data/audio")
    _sftp_mkdir(sftp, f"{REMOTE}/offline")

    offline_tar = REPO / ".cache" / "h2_offline" / "cyclonedds-0.10.2.tar.gz"
    if not offline_tar.is_file():
        print("Bundling offline deps (cyclonedds sdist)...")
        import subprocess

        subprocess.run([sys.executable, str(REPO / "scripts" / "h2_bundle_offline_deps.py")], check=True)
    sftp.put(str(offline_tar), f"{REMOTE}/offline/cyclonedds-0.10.2.tar.gz")
    print("  put offline/cyclonedds-0.10.2.tar.gz")

    for name in SCRIPTS:
        lp = REPO / "scripts" / name
        sftp.put(str(lp), f"{REMOTE}/scripts/{name}")
        print(f"  put scripts/{name}")

    install_sh = REPO / "scripts" / "h2_install_sdk_jetson.sh"
    with sftp.open(f"{REMOTE}/install_sdk.sh", "wb") as rf:
        rf.write(_read_unix_lf(install_sh))
    print("  put install_sdk.sh (LF)")

    jetson_readme = REPO / "docs" / "h2_demo_jetson_README.md"
    with sftp.open(f"{REMOTE}/README.md", "wb") as rf:
        rf.write(_read_unix_lf(jetson_readme))
    print("  put README.md")

    wav = REPO / "data" / "audio" / "cyberpunk_meme.wav"
    if not wav.is_file():
        print(
            "ERROR: missing data/audio/cyberpunk_meme.wav — convert MP3 first:\n"
            '  ffmpeg -y -i "%USERPROFILE%\\Downloads\\cyberpunk-2077.mp3" '
            "-ar 16000 -ac 1 -sample_fmt s16 data/audio/cyberpunk_meme.wav",
            file=sys.stderr,
        )
        return 1
    sftp.put(str(wav), f"{REMOTE}/data/audio/cyberpunk_meme.wav")
    print("  put data/audio/cyberpunk_meme.wav")

    pellegrino = REPO / "data" / "audio" / "pellegrino_tts.wav"
    if not pellegrino.is_file():
        print("Generating pellegrino_tts.wav (Italian edge-tts)...")
        import subprocess

        subprocess.run([sys.executable, str(REPO / "scripts" / "generate_pellegrino_tts_wav.py")], check=True)
    sftp.put(str(pellegrino), f"{REMOTE}/data/audio/pellegrino_tts.wav")
    print("  put data/audio/pellegrino_tts.wav")

    sdk_local = REPO / "unitree_sdk2_python"
    print("Uploading unitree_sdk2_python (may take a minute)...")
    _upload_dir(sftp, sdk_local, f"{REMOTE}/unitree_sdk2_python")
    sftp.close()

    print("Running install_sdk.sh (system CycloneDDS /usr/local)...")
    _, stdout, stderr = c.exec_command(f"bash {REMOTE}/install_sdk.sh 2>&1", timeout=300)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    safe_out = out[-4000:] if len(out) > 4000 else out
    print(safe_out.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace"))
    if err.strip():
        print("stderr:", err[-2000:], file=sys.stderr)
    code = stdout.channel.recv_exit_status()
    # Rimuovi script solo-PC eventualmente lasciati da deploy precedenti
    c.exec_command(f"rm -f {REMOTE}/scripts/h2_smoke_remote.py")
    c.close()
    if code != 0:
        print(f"install failed exit={code}", file=sys.stderr)
        return code

    print(f"\nDeploy OK. Next: python scripts/h2_smoke_remote.py lowstate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

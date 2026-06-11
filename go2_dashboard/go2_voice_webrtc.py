"""Playback TTS sul Go2 via WebRTC (``unitree_webrtc_connect``), come negli esempi RoboVerse.

Il DDS RPC ``voice`` e il PCM stream sono un percorso distinto; molti Go2 in laboratorio espongono
l'audio **inviato** dall'host come track WebRTC (vedi ``examples/go2/audio/mp3_player`` upstream).

Richiede sulla macchina che esegue la dashboard (tipicamente NX): ``pip install unitree-webrtc-connect``
(e dipendenze AV). Lo script ``scripts/pc_go2_webrtc_play_mp3.py`` viene lanciato in subprocess per
non mescolare asyncio WebRTC col loop Flask."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from go2_dashboard.paths import PROJECT_ROOT


def kill_stale_webrtc_play_procs() -> int:
    """Termina eventuali ``pc_go2_webrtc_play_mp3.py`` zombie (un solo slot WebRTC sul Go2)."""
    try:
        proc = subprocess.run(
            ["pkill", "-f", "pc_go2_webrtc_play_mp3.py"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return int(proc.returncode)
    except (OSError, subprocess.TimeoutExpired):
        return -1


def try_play_mp3_bytes_via_webrtc_subprocess(mp3: bytes) -> bool:
    """Se ``GO2_HERMES_PLAY_ON_GO2_WEBRTC=1``, scrive MP3 temporaneo e invoca ``pc_go2_webrtc_play_mp3.py``.

    Env:
        GO2_WEBRTC_IP o UNITREE_ROBOT_IP — IP signaling del Go2 (non la Jetson).
        UNITREE_AES_128_KEY / GO2_WEBRTC_AES_128_KEY — obbligatorio su firmware Go2 ≥ 1.1.15 (data2=3).
        GO2_WEBRTC_AUDIO_SUBPROCESS_TIMEOUT_S — timeout subprocess (default 85).
    """
    flag = (os.environ.get("GO2_HERMES_PLAY_ON_GO2_WEBRTC") or "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return False
    if not mp3:
        return False

    ip = (os.environ.get("GO2_WEBRTC_IP") or os.environ.get("UNITREE_ROBOT_IP") or "").strip()
    if not ip:
        return False

    script = PROJECT_ROOT / "scripts" / "pc_go2_webrtc_play_mp3.py"
    if not script.is_file():
        return False

    kill_stale_webrtc_play_procs()
    time.sleep(0.8)

    try:
        timeout_s = float((os.environ.get("GO2_WEBRTC_AUDIO_SUBPROCESS_TIMEOUT_S") or "85").strip() or "85")
    except ValueError:
        timeout_s = 85.0
    timeout_s = max(30.0, min(timeout_s, 120.0))

    path: Path | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="hermes_webrtc_", suffix=".mp3")
        os.close(fd)
        path = Path(tmp_path)
        path.write_bytes(mp3)
        cmd = [sys.executable, str(script), "--ip", ip, "--file", str(path)]
        try:
            retries = int((os.environ.get("GO2_WEBRTC_AUDIO_RETRIES") or "1").strip() or "1")
        except ValueError:
            retries = 2
        retries = max(1, min(retries, 5))
        try:
            retry_delay_s = float((os.environ.get("GO2_WEBRTC_AUDIO_RETRY_DELAY_S") or "8").strip() or "8")
        except ValueError:
            retry_delay_s = 8.0
        retry_delay_s = max(2.0, min(retry_delay_s, 45.0))

        ok = False
        proc = None
        out = ""
        err = ""
        for attempt in range(retries):
            proc = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                timeout=timeout_s,
                check=False,
                env=os.environ.copy(),
            )
            out = (proc.stdout or b"").decode("utf-8", errors="replace")
            err = (proc.stderr or b"").decode("utf-8", errors="replace")
            ok = proc.returncode == 0 and "webrtc_play_mp3_ok" in out
            if ok:
                break
            if attempt + 1 < retries:
                time.sleep(retry_delay_s)
        if os.environ.get("GO2_GO2_VOICE_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}:
            sys.stderr.write(f"[go2_voice_webrtc] rc={proc.returncode} out={out[-400:]!r} err={err[-800:]!r}\n")
        return ok
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except TypeError:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass


def try_play_b64_mp3_via_webrtc_subprocess(b64_mp3: str) -> bool:
    try:
        raw = base64.b64decode((b64_mp3 or "").strip(), validate=False)
    except Exception:
        return False
    return try_play_mp3_bytes_via_webrtc_subprocess(raw)

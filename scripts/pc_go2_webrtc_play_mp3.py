#!/usr/bin/env python3
"""Riproduce un file MP3 sull'altoparlante del Go2 via **WebRTC** (stesso canale dell'app Unitree).

Basato su ``examples/go2/audio/mp3_player/play_mp3.py`` di
https://github.com/legion1581/unitree_webrtc_connect (community RoboVerse).

Quando il DDS RPC ``voice`` non risponde (es. ``RPC_ERR_CLIENT_SEND``), questo percorso è quello
documentato per **audio send** sul Go2 PRO/AIR/EDU.

Dipendenze sulla macchina che esegue lo script (NX o PC sulla LAN del cane)::

    pip install unitree-webrtc-connect
    # PortAudio consigliato (Linux): sudo apt install portaudio19-dev

Env tipici::

    GO2_WEBRTC_IP o UNITREE_ROBOT_IP   IP signaling del Go2 (es. 192.168.123.161)
    UNITREE_AES_128_KEY               Firmware Go2 ≥ 1.1.15 (data2=3), da ``unitree-fetch-aes-key``

Uso::

    python scripts/pc_go2_webrtc_play_mp3.py --file /tmp/hermes.mp3
    python scripts/pc_go2_webrtc_play_mp3.py --ip 192.168.123.161 --file clip.mp3
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import sys

if sys.platform == "win32":
    import io

    for _name in ("stdout", "stderr"):
        _s = getattr(sys, _name, None)
        if _s is not None and hasattr(_s, "buffer"):
            setattr(
                sys,
                _name,
                io.TextIOWrapper(_s.buffer, encoding="utf-8", errors="replace"),
            )


def _resolve_robot_ip(cli_ip: str) -> str:
    raw = (cli_ip or "").strip()
    if raw:
        return raw
    for key in ("GO2_WEBRTC_IP", "UNITREE_ROBOT_IP"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    return ""


def _resolve_aes_key(cli_key: str) -> str | None:
    raw = (cli_key or "").strip()
    if raw:
        return raw
    for key in ("UNITREE_AES_128_KEY", "GO2_WEBRTC_AES_128_KEY"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    return None


def _media_duration_s(path: str) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return float((os.environ.get("GO2_WEBRTC_AUDIO_DEFAULT_DURATION_S") or "35").strip() or "35")
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return max(3.0, float((proc.stdout or "0").strip()) + 4.0)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return 40.0


async def _run(*, robot_ip: str, mp3_path: str, aes_key: str | None) -> int:
    try:
        from aiortc.contrib.media import MediaPlayer
        from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
        from unitree_webrtc_connect.webrtc_datachannel import WebRTCDataChannel
    except ImportError:
        print(
            "Manca una dipendenza:  python -m pip install unitree-webrtc-connect aiortc av",
            file=sys.stderr,
        )
        return 2

    # Su Jetson ARM la handshake ICE+DTLS+validation può superare i 15s default della libreria.
    try:
        dc_timeout_s = float((os.environ.get("GO2_WEBRTC_DATACHANNEL_TIMEOUT_S") or "60").strip() or "60")
    except ValueError:
        dc_timeout_s = 60.0
    dc_timeout_s = max(15.0, min(dc_timeout_s, 180.0))
    _orig_wait = WebRTCDataChannel.wait_datachannel_open

    async def _wait_datachannel_open(self, timeout: float = 15) -> None:
        return await _orig_wait(self, timeout=dc_timeout_s)

    WebRTCDataChannel.wait_datachannel_open = _wait_datachannel_open  # type: ignore[method-assign]

    kw: dict = {"ip": robot_ip}
    if aes_key:
        kw["aes_128_key"] = aes_key

    conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, **kw)
    logging.basicConfig(level=logging.FATAL)

    try:
        await conn.connect()
        player = MediaPlayer(mp3_path)
        if player.audio is None:
            print("webrtc_play_mp3: MediaPlayer non ha traccia audio", file=sys.stderr)
            return 1

        conn.pc.addTrack(player.audio)
        hold = min(_media_duration_s(mp3_path), float(os.environ.get("GO2_WEBRTC_AUDIO_MAX_WAIT_S") or "180"))
        await asyncio.sleep(hold)
        print("webrtc_play_mp3_ok")
        return 0
    finally:
        try:
            await conn.disconnect()
        except Exception:
            pass
        WebRTCDataChannel.wait_datachannel_open = _orig_wait  # type: ignore[method-assign]


def main() -> int:
    ap = argparse.ArgumentParser(description="Play MP3 on Go2 speaker via WebRTC (unitree_webrtc_connect)")
    ap.add_argument("--ip", default="", metavar="ADDR", help="IP signaling Go2 (default: GO2_WEBRTC_IP / UNITREE_ROBOT_IP)")
    ap.add_argument("--aes-key", default="", metavar="HEX", help="AES-128 key data2=3 (default: UNITREE_AES_128_KEY)")
    ap.add_argument("--file", required=True, metavar="PATH", help="Percorso file MP3")
    args = ap.parse_args()

    ip = _resolve_robot_ip(args.ip)
    if not ip:
        print("Serve --ip oppure GO2_WEBRTC_IP / UNITREE_ROBOT_IP", file=sys.stderr)
        return 2
    path = os.path.abspath(args.file)
    if not os.path.isfile(path):
        print(f"File non trovato: {path}", file=sys.stderr)
        return 2

    aes = _resolve_aes_key(args.aes_key)
    try:
        return asyncio.run(_run(robot_ip=ip, mp3_path=path, aes_key=aes))
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"webrtc_play_mp3_fail:{exc!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

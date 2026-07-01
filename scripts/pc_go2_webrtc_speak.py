#!/usr/bin/env python3
"""TTS veloce (espeak) + casse Go2; attesa playback = durata WAV (niente taglio)."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import uuid as uuid_mod
import wave
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(message)s")
_LOG = logging.getLogger("go2_speak")


def _wav_duration_s(wav: Path) -> float:
    with wave.open(str(wav), "rb") as wf:
        rate = wf.getframerate() or 44100
        frames = wf.getnframes()
        return frames / float(rate)


def _megaphone_wait_s(wav: Path, play_s: float | None) -> float:
    if play_s is not None and play_s >= 0:
        return play_s
    margin = float(os.environ.get("HERMES_PLAY_MARGIN_S", "0.45"))
    min_s = float(os.environ.get("HERMES_MEGAPHONE_PLAY_MIN_S", "0.6"))
    max_s = float(os.environ.get("HERMES_MEGAPHONE_PLAY_MAX_S", "30"))
    dur = _wav_duration_s(wav)
    return max(min_s, min(max_s, dur + margin))


def _tts_wav(text: str, out_wav: Path) -> None:
    from go2_dashboard.hermes.tts_local import synthesize_wav

    if synthesize_wav(text, out_path=out_wav):
        _LOG.info("TTS ok -> %s", out_wav.name)
        return
    raise RuntimeError("TTS fallito (installa espeak-ng: apt install espeak-ng)")


def _unwrap_data(payload: Any) -> Any:
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload
    if isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], str):
            try:
                return json.loads(payload["data"])
            except json.JSONDecodeError:
                pass
        if "data" in payload and isinstance(payload["data"], dict):
            return payload["data"]
    return payload


def _audio_items(payload: Any) -> list[dict[str, Any]]:
    data = _unwrap_data(payload)
    if isinstance(data, dict):
        for key in ("audio_list", "list", "files", "items", "data"):
            raw = data.get(key)
            if isinstance(raw, list):
                return [x for x in raw if isinstance(x, dict)]
        if "file_name" in data or "unique_id" in data:
            return [data]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _pick_play_id(list_resp: Any, file_stem: str) -> str | None:
    items = _audio_items(list_resp)
    for it in reversed(items):
        name = str(it.get("file_name") or it.get("name") or "")
        if name == file_stem or name.startswith(file_stem):
            uid = it.get("unique_id") or it.get("uuid") or it.get("id")
            if uid is not None:
                return str(uid)
    if items:
        last = items[-1]
        uid = last.get("unique_id") or last.get("uuid") or last.get("id")
        if uid is not None:
            return str(uid)
    return None


def _status_ok(resp: Any) -> bool:
    data = _unwrap_data(resp)
    if isinstance(data, dict):
        header = data.get("header") or {}
        status = header.get("status") or {}
        code = status.get("code")
        if code is not None:
            return int(code) == 0
    return True


async def _set_volume(conn: Any, level: int = 8) -> None:
    from unitree_webrtc_connect.constants import RTC_TOPIC

    topic = RTC_TOPIC.get("VUI", "rt/api/vui/request")
    try:
        await conn.datachannel.pub_sub.publish_request_new(
            topic,
            {"api_id": 1003, "parameter": json.dumps({"volume": int(level)})},
        )
    except Exception as exc:
        _LOG.warning("VUI volume skip: %s", exc)


async def _play_audiohub(hub: Any, wav: Path, *, play_s: float) -> bool:
    stem = wav.stem
    up_resp = await hub.upload_audio_file(str(wav))
    play_id = _pick_play_id(up_resp, stem)
    if not play_id:
        lst = await hub.get_audio_list()
        play_id = _pick_play_id(lst, stem)
    if not play_id:
        return False
    await hub.set_play_mode("single_cycle")
    play_resp = await hub.play_by_uuid(play_id)
    ok = _status_ok(play_resp)
    await asyncio.sleep(play_s)
    return ok


async def _play_wav_on_hub(hub: Any, wav: Path, *, play_s: float | None) -> bool:
    wait = _megaphone_wait_s(wav, play_s)
    _LOG.info("Playback wait %.2fs (wav %.2fs)", wait, _wav_duration_s(wav))
    mode = os.environ.get("HERMES_SPEAK_MODE", "megaphone").strip().lower()
    ok = False
    if mode in ("megaphone", "both", "auto"):
        try:
            await hub.enter_megaphone()
            await hub.upload_megaphone(str(wav))
            await asyncio.sleep(wait)
            await hub.exit_megaphone()
            ok = True
        except Exception as exc:
            _LOG.warning("Megaphone failed: %s", exc)
    if not ok and mode in ("audiohub", "both", "auto"):
        ok = await _play_audiohub(hub, wav, play_s=wait)
    return ok


async def _speak_wav(ip: str, wav: Path, *, use_ap: bool, play_s: float | None) -> int:
    from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
    from unitree_webrtc_connect.webrtc_audiohub import WebRTCAudioHub

    if use_ap:
        conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalAP)
    else:
        conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=ip.strip())

    try:
        await _connect_with_retry(conn, label="single")
    except Exception as exc:
        _LOG.error("WebRTC connect failed: %s", exc)
        return 1
    hub = WebRTCAudioHub(conn)
    vol = int(os.environ.get("HERMES_GO2_VOLUME", "8"))
    await _set_volume(conn, vol)
    ok = await _play_wav_on_hub(hub, wav, play_s=play_s)
    await conn.disconnect()
    return 0 if ok else 1


async def _speak_text(ip: str, text: str, *, use_ap: bool, play_s: float | None) -> int:
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / f"hermes_{uuid_mod.uuid4().hex[:8]}.wav"
        _tts_wav(text, wav)
        return await _speak_wav(ip, wav, use_ap=use_ap, play_s=play_s)


async def _connect_with_retry(conn: Any, *, label: str) -> None:
    retries = int(os.environ.get("HERMES_WEBRTC_CONNECT_RETRIES", "3"))
    delay_s = float(os.environ.get("HERMES_WEBRTC_RETRY_DELAY_S", "6"))
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            await conn.connect()
            return
        except Exception as exc:
            last_exc = exc
            _LOG.warning("%s connect attempt %d/%d failed: %s", label, attempt, retries, exc)
            try:
                await conn.disconnect()
            except Exception:
                pass
            if attempt < retries:
                await asyncio.sleep(delay_s)
    if last_exc:
        raise last_exc


async def _speak_playlist(
    ip: str,
    wav_paths: list[Path],
    pauses_after: list[float],
    *,
    use_ap: bool,
) -> int:
    from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
    from unitree_webrtc_connect.webrtc_audiohub import WebRTCAudioHub

    if not wav_paths:
        return 2
    if use_ap:
        conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalAP)
    else:
        conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=ip.strip())

    _LOG.info("WebRTC playlist %d clip -> %s", len(wav_paths), ip if not use_ap else "AP")
    try:
        await _connect_with_retry(conn, label="playlist")
    except Exception as exc:
        _LOG.error("WebRTC connect failed: %s", exc)
        return 1
    hub = WebRTCAudioHub(conn)
    vol = int(os.environ.get("HERMES_GO2_VOLUME", "8"))
    await _set_volume(conn, vol)

    all_ok = True
    for idx, wav in enumerate(wav_paths):
        _LOG.info("Clip %d/%d %s", idx + 1, len(wav_paths), wav.name)
        ok = await _play_wav_on_hub(hub, wav, play_s=None)
        all_ok = all_ok and ok
        pause = pauses_after[idx] if idx < len(pauses_after) else 0.0
        if pause > 0:
            _LOG.info("Pausa %.1fs prima del prossimo clip", pause)
            await asyncio.sleep(pause)

    await conn.disconnect()
    if not all_ok:
        _LOG.error("PLAYLIST_FAILED")
        return 1
    _LOG.info("PLAYLIST_OK")
    return 0


def _parse_pause_csv(raw: str, n: int) -> list[float]:
    if not raw.strip():
        return [0.0] * n
    parts = [float(x.strip()) for x in raw.split(",") if x.strip()]
    while len(parts) < n:
        parts.append(0.0)
    return parts[:n]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default=(os.environ.get("GO2_WEBRTC_IP") or "192.168.123.161").strip())
    ap.add_argument("--text", default="")
    ap.add_argument("--wav", default="")
    ap.add_argument("--playlist", default="", help="wav1,wav2,... una sessione WebRTC")
    ap.add_argument("--pause-after", default="", help="secondi di pausa dopo ogni clip, es. 0,12,0")
    ap.add_argument(
        "--play-s",
        type=float,
        default=None,
        help="secondi dopo upload; omit = durata WAV automatica",
    )
    ap.add_argument("--ap", action="store_true")
    args = ap.parse_args()
    if args.play_s is None:
        raw = (os.environ.get("HERMES_MEGAPHONE_PLAY_S") or "").strip()
        if raw:
            try:
                v = float(raw)
                if v > 0:
                    args.play_s = v
            except ValueError:
                pass
    try:
        import unitree_webrtc_connect  # noqa: F401
    except ImportError:
        print("pip install unitree-webrtc-connect gTTS", file=sys.stderr)
        raise SystemExit(2)

    if args.playlist.strip():
        paths = [Path(p.strip()) for p in args.playlist.split(",") if p.strip()]
        for p in paths:
            if not p.is_file():
                print(f"WAV missing: {p}", file=sys.stderr)
                raise SystemExit(2)
        pauses = _parse_pause_csv(args.pause_after, len(paths))
        coro = _speak_playlist(args.ip, paths, pauses, use_ap=args.ap)
    elif args.wav.strip():
        wav = Path(args.wav.strip())
        if not wav.is_file():
            print(f"WAV missing: {wav}", file=sys.stderr)
            raise SystemExit(2)
        coro = _speak_wav(args.ip, wav, use_ap=args.ap, play_s=args.play_s)
    elif args.text.strip():
        coro = _speak_text(args.ip, args.text.strip(), use_ap=args.ap, play_s=args.play_s)
    else:
        ap.error("serve --text, --wav o --playlist")
    raise SystemExit(asyncio.run(coro))


if __name__ == "__main__":
    main()

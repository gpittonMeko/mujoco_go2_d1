"""TTS sul Go2: percorso SDK2 reale.

L'SDK Python **non** espone un ``go2/audio`` dedicato: sul campo si usa lo stesso stack **RPC DDS**
``voice`` registrato come sul G1 (``PlayStream`` / ``PlayStop``), con versione API firmware‑dipendente.

Fallback **opzionale**: pubblicazione PCM come ``unitree_go.msg.dds_.AudioData_`` su un topic DDS scelto
(``GO2_GO2_AUDIO_DDS_TOPIC``). Il nome topic **non** è documentato in modo univoco nel repo upstream per
tutte le revisioni Go2: va verificato sul robot / firmware oppure lasciato vuoto (solo RPC).

PCM 16‑bit LE, sample rate da env (default 16 kHz mono). MP3 OpenAI → PCM tramite ``ffmpeg`` sulla NX."""

from __future__ import annotations

import base64
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from typing import Any

from go2_dashboard.operator_stack import go2_local
from go2_dashboard.paths import PROJECT_ROOT

_APP_NAME = "hermes_tts"
_CLIENT_LOCK = threading.Lock()
_RPC_CLIENT = None
_RPC_VER = ""

_DDS_PUB_LOCK = threading.Lock()
_DDS_PUB = None
_DDS_PUB_TOPIC = ""


def _dbg(msg: str) -> None:
    if os.environ.get("GO2_GO2_VOICE_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}:
        print(f"[go2_voice_playback] {msg}", file=sys.stderr)


def synthetic_beep_mp3_bytes(*, duration_s: float = 0.35, freq_hz: int = 880) -> bytes | None:
    """MP3 curtissimo (sine) via ffmpeg — utile per ``/api/robot/voice_test`` senza OpenAI."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    dur = max(0.05, min(float(duration_s), 3.0))
    fq = max(200, min(int(freq_hz), 4000))
    cmd = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={fq}:sample_rate=44100:duration={dur}",
        "-f",
        "mp3",
        "-acodec",
        "libmp3lame",
        "-b:a",
        "64k",
        "-ac",
        "1",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        cmd2 = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={fq}:sample_rate=44100:duration={dur}",
            "-f",
            "mp3",
            "-ac",
            "1",
            "pipe:1",
        ]
        try:
            proc = subprocess.run(cmd2, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not proc.stdout:
            return None
    return proc.stdout


def pure_python_beep_pcm_s16le_mono(
    *,
    duration_s: float = 0.35,
    freq_hz: int = 880,
    sample_rate: int = 16000,
) -> bytes:
    """PCM sintetico senza ffmpeg (fallback sulla NX se lavfi/lame non disponibili)."""
    dur = max(0.05, min(float(duration_s), 3.0))
    fq = float(max(200, min(int(freq_hz), 8000)))
    sr = float(max(8000, min(int(sample_rate), 48000)))
    n = int(dur * sr)
    out = bytearray()
    amp = 5200.0
    for i in range(n):
        s = int(amp * math.sin(2.0 * math.pi * fq * (i / sr)))
        s = max(-32768, min(32767, s))
        out.extend(struct.pack("<h", s))
    return bytes(out)


def synthetic_beep_pcm_s16le_mono_bytes(
    *,
    duration_s: float = 0.35,
    freq_hz: int = 880,
    sample_rate: int = 16000,
) -> bytes | None:
    """PCM sintetico (sine) via ffmpeg — non richiede libmp3lame (a differenza del MP3)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    dur = max(0.05, min(float(duration_s), 3.0))
    fq = max(200, min(int(freq_hz), 8000))
    sr = max(8000, min(int(sample_rate), 48000))
    cmd = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={fq}:sample_rate={sr}:duration={dur}",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        str(sr),
        "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def _api_version_candidates() -> list[str]:
    primary = (os.environ.get("GO2_GO2_VOICE_API_VERSION") or "1.0.0.0").strip()
    raw_fb = (os.environ.get("GO2_GO2_VOICE_API_VERSION_FALLBACKS") or "").strip()
    extra: list[str] = []
    if raw_fb:
        extra = [p.strip() for p in raw_fb.split(",") if p.strip()]
    if os.environ.get("GO2_GO2_VOICE_TRY_EXTRA_API_VERSIONS", "").strip().lower() in {"1", "true", "yes", "on"}:
        extra.extend(["1.0.0.1", "1.0.0.2"])
    out: list[str] = []
    for v in [primary] + extra:
        if v and v not in out:
            out.append(v)
    return out


def _clear_rpc_cache_unlocked() -> None:
    global _RPC_CLIENT, _RPC_VER
    _RPC_CLIENT = None
    _RPC_VER = ""


def _fill_get_volume_diag(client, diag: dict[str, Any]) -> None:
    try:
        gv = client.GetVolume()
        code = gv[0] if isinstance(gv, tuple) else gv
        diag["get_volume_code"] = int(code)
        diag["get_volume_data"] = gv[1] if isinstance(gv, tuple) and len(gv) > 1 else None
    except Exception as exc:
        diag["get_volume_error"] = repr(exc)


def _ensure_rpc_client(api_ver: str, diag: dict[str, Any] | None = None):
    """Costruisce ``AudioClient`` con ``AUDIO_API_VERSION`` impostata; ``GetVolume`` come sanity check."""
    global _RPC_CLIENT, _RPC_VER
    with _CLIENT_LOCK:
        if _RPC_CLIENT is not None and _RPC_VER == api_ver:
            if diag is not None:
                _fill_get_volume_diag(_RPC_CLIENT, diag)
            return _RPC_CLIENT
        _clear_rpc_cache_unlocked()
        try:
            sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
            from go2_accompany import ensure_go2_dds_channel_factory_from_env

            ensure_go2_dds_channel_factory_from_env(PROJECT_ROOT)
            import unitree_sdk2py.g1.audio.g1_audio_api as audio_api

            audio_api.AUDIO_API_VERSION = api_ver
            from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

            c = AudioClient()
            c.SetTimeout(float(os.environ.get("GO2_GO2_VOICE_RPC_TIMEOUT_S", "8")))
            c.Init()
            gv = c.GetVolume()
            code = gv[0] if isinstance(gv, tuple) else gv
            if diag is not None:
                diag["get_volume_code"] = int(code)
                diag["get_volume_data"] = gv[1] if isinstance(gv, tuple) and len(gv) > 1 else None
            if code != 0:
                _dbg(f"GetVolume nok api={api_ver} code={code}")
                return None
            _RPC_CLIENT = c
            _RPC_VER = api_ver
            _dbg(f"RPC voice client ok api={api_ver}")
            return c
        except Exception as exc:
            _dbg(f"RPC init exception api={api_ver}: {exc}")
            if diag is not None:
                diag["init_error"] = repr(exc)
            _clear_rpc_cache_unlocked()
            return None


def _invalidate_rpc_after_play_failure() -> None:
    with _CLIENT_LOCK:
        _clear_rpc_cache_unlocked()


def _mp3_bytes_to_pcm_s16le_mono(mp3: bytes, sample_rate: int) -> bytes | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    cmd = [
        ffmpeg,
        "-nostdin",
        "-i",
        "pipe:0",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ar",
        str(int(sample_rate)),
        "-ac",
        "1",
        "-loglevel",
        "error",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, input=mp3, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def _stream_pcm_play_rpc(
    client,
    pcm: bytes,
    *,
    chunk_bytes: int,
    bytes_per_sec: int,
    telemetry: dict[str, Any] | None = None,
) -> bool:
    tel = telemetry if telemetry is not None else {}
    tel.setdefault("play_stream_codes", [])
    tel["chunks_pcm"] = 0
    stream_id = str(int(time.time() * 1000))
    try:
        client.PlayStop(_APP_NAME)
    except Exception:
        pass
    offset = 0
    total = len(pcm)
    if total == 0:
        return False
    while offset < total:
        chunk = pcm[offset : offset + chunk_bytes]
        if not chunk:
            break
        ret = client.PlayStream(_APP_NAME, stream_id, chunk)
        code = ret[0] if isinstance(ret, tuple) else ret
        tel["chunks_pcm"] += 1
        tel["play_stream_codes"].append(int(code))
        if code != 0:
            _dbg(f"PlayStream code={code} offset={offset}/{total}")
            tel["failed_offset_bytes"] = offset
            return False
        dur = len(chunk) / float(bytes_per_sec)
        time.sleep(max(dur * 0.88, 0.02))
        offset += len(chunk)
    tail = min(total / float(bytes_per_sec), 8.0)
    time.sleep(max(tail * 0.15, 0.05))
    try:
        client.PlayStop(_APP_NAME)
    except Exception:
        pass
    return True


def _dds_publisher(topic: str):
    global _DDS_PUB, _DDS_PUB_TOPIC
    with _DDS_PUB_LOCK:
        if _DDS_PUB is not None and _DDS_PUB_TOPIC == topic:
            return _DDS_PUB
        if _DDS_PUB is not None:
            try:
                _DDS_PUB.Close()
            except Exception:
                pass
            _DDS_PUB = None
            _DDS_PUB_TOPIC = ""
        from unitree_sdk2py.core.channel import ChannelPublisher
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import AudioData_

        pub = ChannelPublisher(topic, AudioData_)
        pub.Init()
        _DDS_PUB = pub
        _DDS_PUB_TOPIC = topic
        _dbg(f"DDS AudioData_ publisher on {topic!r}")
        return pub


def _stream_pcm_play_dds(
    pcm: bytes,
    *,
    topic: str,
    chunk_bytes: int,
    bytes_per_sec: int,
    telemetry: dict[str, Any] | None = None,
) -> bool:
    tel = telemetry if telemetry is not None else {}
    tel["frames_written"] = 0
    tel["topic"] = topic
    try:
        pub = _dds_publisher(topic)
    except Exception as exc:
        _dbg(f"DDS publisher failed: {exc}")
        tel["publisher_error"] = repr(exc)
        return False
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import AudioData_

    tf = 0
    offset = 0
    total = len(pcm)
    if total == 0:
        return False
    while offset < total:
        chunk = pcm[offset : offset + chunk_bytes]
        if not chunk:
            break
        tf += 1
        msg = AudioData_(tf, list(chunk))
        try:
            pub.Write(msg)
        except Exception as exc:
            _dbg(f"DDS Write failed tf={tf}: {exc}")
            tel["write_error_frame"] = tf
            tel["write_error"] = repr(exc)
            return False
        tel["frames_written"] += 1
        dur = len(chunk) / float(bytes_per_sec)
        time.sleep(max(dur * 0.88, 0.02))
        offset += len(chunk)
    tail = min(total / float(bytes_per_sec), 8.0)
    time.sleep(max(tail * 0.15, 0.05))
    return True


def _voice_transport_mode() -> str:
    raw = (os.environ.get("GO2_GO2_VOICE_TRANSPORT") or "rpc").strip().lower()
    if raw in {"rpc", "dds", "auto"}:
        return raw
    return "rpc"


def _dds_topic_configured() -> str:
    return (os.environ.get("GO2_GO2_AUDIO_DDS_TOPIC") or "").strip()


def go2_voice_playback_report(
    b64_mp3: str | None = None,
    *,
    pcm_s16le_mono: bytes | None = None,
) -> dict[str, Any]:
    """Esegue playback sul Go2 e restituisce diagnostica JSON‑safe.

    **robot_voice_rpc_ack**: tutti i codici di risposta ``PlayStream`` (RPC verso il servizio ``voice`` sul cane)
    sono ``0`` — è il feedback SDK più vicino a «il cane ha accettato lo stream PCM».

    Passare **oppure** ``b64_mp3`` **oppure** ``pcm_s16le_mono`` (stesso sample rate di ``GO2_GO2_VOICE_SAMPLE_RATE``).
    """
    rep: dict[str, Any] = {
        "success": False,
        "skipped": True,
        "skip_reason": None,
        "audio_source": None,
        "go2_local": go2_local(),
        "play_on_go2_env": (os.environ.get("GO2_HERMES_PLAY_ON_GO2") or "").strip().lower()
        in {"1", "true", "yes", "on"},
        "transport_mode": _voice_transport_mode(),
        "dds_topic_configured": _dds_topic_configured() or None,
        "ffmpeg_available": bool(shutil.which("ffmpeg")),
        "mp3_bytes": 0,
        "pcm_bytes": 0,
        "sample_rate": None,
        "robot_voice_rpc_ack": False,
        "robot_voice_dds_completed": False,
        "rpc": None,
        "dds": None,
        "error": None,
    }

    flag = (os.environ.get("GO2_HERMES_PLAY_ON_GO2") or "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        rep["skip_reason"] = "GO2_HERMES_PLAY_ON_GO2_disabled"
        return rep
    if not go2_local():
        rep["skip_reason"] = "GO2_LOCAL_off"
        return rep

    try:
        sample_rate = int((os.environ.get("GO2_GO2_VOICE_SAMPLE_RATE") or "16000").strip())
    except ValueError:
        sample_rate = 16000
    if sample_rate <= 0:
        sample_rate = 16000
    rep["sample_rate"] = sample_rate

    pcm: bytes | None = None
    if pcm_s16le_mono is not None:
        rep["audio_source"] = "pcm_s16le_inline"
        if len(pcm_s16le_mono) < 4:
            rep["skip_reason"] = "pcm_too_short"
            return rep
        if len(pcm_s16le_mono) % 2 != 0:
            rep["skip_reason"] = "pcm_odd_byte_length"
            return rep
        pcm = pcm_s16le_mono
        rep["skipped"] = False
        rep["pcm_bytes"] = len(pcm)
    elif b64_mp3 and isinstance(b64_mp3, str) and b64_mp3.strip():
        rep["audio_source"] = "mp3_base64"
        try:
            mp3 = base64.b64decode(b64_mp3.strip(), validate=False)
        except Exception as exc:
            rep["skip_reason"] = "base64_decode_failed"
            rep["error"] = repr(exc)
            return rep
        if not mp3:
            rep["skip_reason"] = "empty_mp3"
            return rep
        rep["mp3_bytes"] = len(mp3)
        rep["skipped"] = False
        pcm = _mp3_bytes_to_pcm_s16le_mono(mp3, sample_rate)
        if not pcm:
            rep["skip_reason"] = "pcm_decode_failed_or_no_ffmpeg"
            return rep
        rep["pcm_bytes"] = len(pcm)
    else:
        rep["skip_reason"] = "missing_audio_input"
        return rep

    assert pcm is not None

    try:
        chunk_ms = int((os.environ.get("GO2_GO2_VOICE_CHUNK_MS") or "600").strip())
    except ValueError:
        chunk_ms = 600
    chunk_ms = max(80, min(chunk_ms, 3000))
    bytes_per_sec = sample_rate * 2
    chunk_bytes = max((bytes_per_sec * chunk_ms) // 1000, 4096)
    chunk_bytes = min(chunk_bytes, 96000)
    chunk_bytes -= chunk_bytes % 2

    try:
        dds_chunk = int((os.environ.get("GO2_GO2_AUDIO_DDS_CHUNK_BYTES") or str(chunk_bytes)).strip())
    except ValueError:
        dds_chunk = chunk_bytes
    dds_chunk = max(512, min(dds_chunk, 96000))
    dds_chunk -= dds_chunk % 2

    mode = _voice_transport_mode()
    topic = _dds_topic_configured()

    rpc_summary: dict[str, Any] = {
        "attempted": False,
        "api_versions_tried": [],
        "winning_api_version": None,
        "get_volume_code": None,
        "get_volume_data": None,
        "play_stream_codes": [],
        "chunks_pcm": 0,
    }
    dds_summary: dict[str, Any] = {"attempted": False, "topic": topic or None, "frames_written": 0, "success": False}

    def play_rpc() -> bool:
        rpc_summary["attempted"] = True
        for ver in _api_version_candidates():
            rpc_summary["api_versions_tried"].append(ver)
            diag: dict[str, Any] = {}
            client = _ensure_rpc_client(ver, diag)
            if client is None:
                rpc_summary.setdefault("client_init_failed", []).append({"api": ver, **diag})
                continue
            rpc_summary["winning_api_version"] = ver
            rpc_summary["get_volume_code"] = diag.get("get_volume_code")
            rpc_summary["get_volume_data"] = diag.get("get_volume_data")
            tel: dict[str, Any] = {}
            ok = _stream_pcm_play_rpc(client, pcm, chunk_bytes=chunk_bytes, bytes_per_sec=bytes_per_sec, telemetry=tel)
            rpc_summary["play_stream_codes"] = list(tel.get("play_stream_codes") or [])
            rpc_summary["chunks_pcm"] = int(tel.get("chunks_pcm") or 0)
            codes = rpc_summary["play_stream_codes"]
            rep["robot_voice_rpc_ack"] = bool(ok and codes and all(c == 0 for c in codes))
            if ok:
                return True
            _invalidate_rpc_after_play_failure()
            rpc_summary["last_play_failure"] = {
                "failed_offset_bytes": tel.get("failed_offset_bytes"),
                "codes_so_far": codes,
            }
        rep["robot_voice_rpc_ack"] = False
        return False

    def play_dds() -> bool:
        if not topic:
            return False
        dds_summary["attempted"] = True
        tel: dict[str, Any] = {}
        ok = _stream_pcm_play_dds(
            pcm, topic=topic, chunk_bytes=dds_chunk, bytes_per_sec=bytes_per_sec, telemetry=tel
        )
        dds_summary["frames_written"] = int(tel.get("frames_written") or 0)
        dds_summary["success"] = ok
        rep["robot_voice_dds_completed"] = bool(ok)
        return ok

    rep["rpc"] = rpc_summary
    rep["dds"] = dds_summary

    try:
        if mode == "dds":
            rep["success"] = play_dds()
        elif mode == "rpc":
            rep["success"] = play_rpc()
        else:
            if play_rpc():
                rep["success"] = True
            else:
                rep["success"] = play_dds()
        return rep
    except Exception as exc:
        rep["error"] = repr(exc)
        rep["success"] = False
        _dbg(f"playback exception: {exc}")
        _invalidate_rpc_after_play_failure()
        return rep


def go2_voice_ttsmaker_report(text: str, *, speaker_id: int | None = None) -> dict[str, Any]:
    """RPC ``voice`` → ``AudioClient.TtsMaker`` (stesso entrypoint degli esempi G1 in ``unitree_sdk2_python``).

    Non è il progetto biscuit (che usa un callback TTS esterno): qui usiamo solo lo stack SDK Unitree.
    ``speaker_id``: ``0`` = auto (CN/EN), ``1`` = inglese (come da doc esempio SDK).
    """
    rep: dict[str, Any] = {
        "success": False,
        "skipped": True,
        "skip_reason": None,
        "audio_source": "ttsmaker_rpc",
        "go2_local": go2_local(),
        "play_on_go2_env": (os.environ.get("GO2_HERMES_PLAY_ON_GO2") or "").strip().lower()
        in {"1", "true", "yes", "on"},
        "text_len": 0,
        "speaker_id": None,
        "robot_ttsmaker_code": None,
        "robot_ttsmaker_ack": False,
        "winning_api_version": None,
        "api_versions_tried": [],
        "client_init_failed": [],
        "error": None,
    }
    flag = (os.environ.get("GO2_HERMES_PLAY_ON_GO2") or "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        rep["skip_reason"] = "GO2_HERMES_PLAY_ON_GO2_disabled"
        return rep
    if not go2_local():
        rep["skip_reason"] = "GO2_LOCAL_off"
        return rep

    if speaker_id is None:
        try:
            sid = int((os.environ.get("GO2_GO2_VOICE_TTS_SPEAKER_ID") or "1").strip())
        except ValueError:
            sid = 1
    else:
        sid = int(speaker_id)
    rep["speaker_id"] = sid

    t = " ".join((text or "").split())
    if not t:
        rep["skip_reason"] = "empty_text"
        return rep
    try:
        max_c = int((os.environ.get("GO2_GO2_VOICE_TTSMAKER_MAX_CHARS") or "240").strip())
    except ValueError:
        max_c = 240
    max_c = max(20, min(max_c, 2000))
    if len(t) > max_c:
        t = t[: max_c - 3] + "..."
    rep["text_len"] = len(t)
    rep["skipped"] = False

    for ver in _api_version_candidates():
        rep["api_versions_tried"].append(ver)
        diag: dict[str, Any] = {}
        client = _ensure_rpc_client(ver, diag)
        if client is None:
            rep.setdefault("client_init_failed", []).append({"api": ver, **diag})
            continue
        rep["winning_api_version"] = ver
        try:
            code = int(client.TtsMaker(t, sid))
        except Exception as exc:
            rep["error"] = repr(exc)
            _invalidate_rpc_after_play_failure()
            continue
        rep["robot_ttsmaker_code"] = code
        rep["robot_ttsmaker_ack"] = code == 0
        rep["success"] = code == 0
        return rep

    rep["success"] = False
    rep["robot_ttsmaker_ack"] = False
    return rep


def try_play_mp3_on_unitree_voice_service(b64_mp3: str) -> bool:
    """Se ``GO2_HERMES_PLAY_ON_GO2=1`` e ``GO2_LOCAL=1``, riproduce MP3 sul Go2 (RPC e/o DDS da env)."""
    return bool(go2_voice_playback_report(b64_mp3).get("success"))

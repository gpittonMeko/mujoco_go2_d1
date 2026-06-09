"""Voce Go2: playlist WAV in una sola sessione WebRTC (ack + pausa + done)."""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from go2_dashboard.hermes.phrases import CANNED_KEYS, PHRASES

_LOG = logging.getLogger("hermes.speech")
_ROOT = Path(__file__).resolve().parent.parent.parent
_SPEAK = _ROOT / "scripts" / "pc_go2_webrtc_speak.py"
_CANNED_DIR = Path(__file__).resolve().parent / "canned"
_SPEAK_LOG = _ROOT / "hermes_speak.log"

_QUEUE: queue.Queue[dict[str, Any]] = queue.Queue()
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
_PLAY_LOCK = threading.Lock()
_LAST_SPEAK: dict[str, Any] = {"ok": True, "skipped": True}
_SPEAKING = False


def _queue_max() -> int:
    return max(1, int(os.environ.get("HERMES_SPEAK_QUEUE_MAX", "1")))


def clear_voice_queue(*, kill_webrtc: bool = True) -> int:
    """Svuota coda in attesa (ferma il loop di messaggi vecchi)."""
    dropped = 0
    while True:
        try:
            _QUEUE.get_nowait()
            _QUEUE.task_done()
            dropped += 1
        except queue.Empty:
            break
    if kill_webrtc:
        try:
            subprocess.run(
                ["pkill", "-f", "pc_go2_webrtc_speak.py"],
                capture_output=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
    _log_speak("queue_cleared", {"dropped": dropped, "kill_webrtc": kill_webrtc})
    return dropped


def _enqueue_task(task: dict[str, Any]) -> None:
    if os.environ.get("HERMES_SPEAK_CLEAR_BEFORE", "1").lower() in {"1", "true", "yes", "on"}:
        while _QUEUE.qsize() >= _queue_max():
            try:
                old = _QUEUE.get_nowait()
                _QUEUE.task_done()
                _log_speak("queue_drop", {"reason": "max_size", "task": list(old.keys())})
            except queue.Empty:
                break
    _QUEUE.put(task)


def interaction_voice_enabled() -> bool:
    return os.environ.get("HERMES_INTERACTION_VOICE", "1").lower() in {"1", "true", "yes", "on"}


def canned_dir() -> Path:
    override = (os.environ.get("HERMES_CANNED_DIR") or "").strip()
    return Path(override) if override else _CANNED_DIR


def phrase_text(key: str) -> str:
    for group in PHRASES.values():
        for slot in ("ack_key", "done_key", "fail_key"):
            if group.get(slot) == key:
                field = slot.replace("_key", "")
                return str(group[field])
    return key.replace("_", " ")


def canned_wav_path(key: str) -> Path | None:
    p = canned_dir() / f"{key}.wav"
    return p if p.is_file() and p.stat().st_size > 100 else None


def _log_speak(event: str, detail: dict[str, Any]) -> None:
    line = json.dumps({"ts": time.time(), "event": event, **detail}, ensure_ascii=False)
    try:
        with open(_SPEAK_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    _LOG.info("%s %s", event, detail)


def _ensure_worker() -> None:
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return

        def _loop() -> None:
            global _LAST_SPEAK, _SPEAKING
            while True:
                task = _QUEUE.get()
                try:
                    with _PLAY_LOCK:
                        _SPEAKING = True
                        if task.get("text"):
                            text = str(task["text"]).strip()
                            _log_speak("text_start", {"chars": len(text)})
                            _LAST_SPEAK = _speak_text_sync(text)
                            _log_speak("text_end", _LAST_SPEAK)
                        else:
                            _LAST_SPEAK = _run_playlist_task(task)
                except Exception as exc:
                    _LAST_SPEAK = {"ok": False, "reason": repr(exc)}
                    _log_speak("voice_error", {"reason": repr(exc)})
                finally:
                    _SPEAKING = False
                    _QUEUE.task_done()

        threading.Thread(target=_loop, name="hermes-voice-queue", daemon=True).start()
        _WORKER_STARTED = True


def _keys_to_wavs(keys: list[str]) -> list[Path]:
    out: list[Path] = []
    for key in keys:
        p = canned_wav_path(key)
        if p:
            out.append(p)
        else:
            _log_speak("canned_missing", {"key": key})
    return out


def _webrtc_cmd_base() -> list[str]:
    raw_ips = (os.environ.get("GO2_WEBRTC_IP") or "192.168.123.161").strip()
    ips = [p.strip() for p in raw_ips.split(",") if p.strip()]
    ip = ips[0] if ips else "192.168.123.161"
    cmd = [sys.executable, str(_SPEAK), "--ip", ip]
    if os.environ.get("GO2_WEBRTC_AP", "").lower() in {"1", "true", "yes"}:
        cmd.append("--ap")
    return cmd


def _task_wavs(task: dict[str, Any]) -> list[Path]:
    wavs = _keys_to_wavs(list(task.get("keys") or []))
    for raw in task.get("wav_paths") or []:
        p = Path(str(raw))
        if p.is_file() and p.stat().st_size > 100:
            wavs.append(p)
        else:
            _log_speak("wav_missing", {"path": str(raw)})
    return wavs


def _run_playlist_task(task: dict[str, Any]) -> dict[str, Any]:
    keys: list[str] = list(task.get("keys") or [])
    pauses: list[float] = list(task.get("pauses_after") or [])
    wavs = _task_wavs(task)
    if not wavs:
        return {"ok": False, "reason": "no_wav", "keys": keys}
    while len(pauses) < len(wavs):
        pauses.append(0.0)
    playlist = ",".join(str(p) for p in wavs)
    pause_csv = ",".join(str(float(x)) for x in pauses[: len(wavs)])
    cmd = _webrtc_cmd_base() + ["--playlist", playlist, "--pause-after", pause_csv]
    t0 = time.perf_counter()
    _log_speak("playlist_start", {"keys": keys, "pauses": pauses[: len(wavs)]})
    out = _run_speak_cmd(cmd)
    out["elapsed_s"] = round(time.perf_counter() - t0, 2)
    out["keys"] = keys
    _log_speak("playlist_end", out)
    return out


def play_script_async(
    keys: list[str],
    pauses_after: list[float] | None = None,
    *,
    wav_paths: list[Path | str] | None = None,
) -> None:
    if os.environ.get("HERMES_SPEAK_DISABLE", "").lower() in {"1", "true", "yes"}:
        return
    _ensure_worker()
    pauses = list(pauses_after or [])
    extra = [str(p) for p in (wav_paths or [])]
    _enqueue_task({"keys": keys, "wav_paths": extra, "pauses_after": pauses})


def play_wav_paths_async(wav_paths: list[Path | str], pauses_after: list[float] | None = None) -> None:
    play_script_async([], pauses_after=pauses_after, wav_paths=wav_paths)


def play_script_sync(keys: list[str], pauses_after: list[float] | None = None) -> dict[str, Any]:
    with _PLAY_LOCK:
        return _run_playlist_task({"keys": keys, "pauses_after": list(pauses_after or [])})


def speak_phrase_immediate(key: str) -> None:
    play_script_async([key], [0.0])


def enqueue_phrase(key: str, *, front: bool = False) -> None:
    del front
    play_script_async([key], [0.0])


def enqueue_text(text: str) -> None:
    if os.environ.get("HERMES_SPEAK_DISABLE", "").lower() in {"1", "true", "yes"}:
        return
    text = (text or "").strip()
    if not text:
        return
    max_chars = int(os.environ.get("HERMES_SPEAK_MAX_CHARS", "220"))
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    _ensure_worker()
    _enqueue_task({"text": text})


def _run_speak_cmd(cmd: list[str]) -> dict[str, Any]:
    if not _SPEAK.is_file():
        return {"ok": False, "reason": "missing pc_go2_webrtc_speak.py"}
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=float(os.environ.get("HERMES_SPEAK_TIMEOUT_S", "120")),
            encoding="utf-8",
            errors="replace",
        )
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": (proc.stdout or "")[-500:],
            "stderr": (proc.stderr or "")[-500:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout"}


def _speak_text_sync(text: str) -> dict[str, Any]:
    cmd = _webrtc_cmd_base() + ["--text", text]
    return _run_speak_cmd(cmd)


def speak(text: str, *, async_mode: bool | None = None) -> dict[str, Any]:
    if async_mode is None:
        async_mode = os.environ.get("HERMES_SPEAK_ASYNC", "1").lower() in {"1", "true", "yes", "on"}
    if not async_mode:
        return _speak_text_sync(text)
    enqueue_text(text)
    return {"ok": True, "async": True, "queued": True}


def last_speak_status() -> dict[str, Any]:
    return dict(_LAST_SPEAK)


def list_canned_status() -> dict[str, Any]:
    d = canned_dir()
    return {
        "dir": str(d),
        "keys": {k: canned_wav_path(k) is not None for k in CANNED_KEYS},
        "speak_log": str(_SPEAK_LOG),
        "queue_size": _QUEUE.qsize(),
        "speaking": _SPEAKING,
    }


def voice_queue_status() -> dict[str, Any]:
    return {
        "queue_size": _QUEUE.qsize(),
        "speaking": _SPEAKING,
        "queue_max": _queue_max(),
    }

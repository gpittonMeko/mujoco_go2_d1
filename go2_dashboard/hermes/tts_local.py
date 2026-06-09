"""TTS Hermes: Piper (binario arm64 + modello ONNX) → gTTS solo fallback esplicito."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("hermes.tts")
_LAST_ENGINE: str = ""

_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parent.parent
_DEFAULT_PIPER_DIR = _ROOT / "piper"
_DEFAULT_PIPER_BIN_DIR = _REPO / "bin" / "piper"


def _espeak_exe() -> str | None:
    return shutil.which("espeak-ng") or shutil.which("espeak")


def piper_model_paths() -> tuple[Path, Path] | None:
    voice = (os.environ.get("HERMES_PIPER_VOICE") or "it_IT-paola-medium").strip()
    base = Path((os.environ.get("HERMES_PIPER_DIR") or "").strip() or _DEFAULT_PIPER_DIR)
    onnx = base / f"{voice}.onnx"
    cfg = base / f"{voice}.onnx.json"
    if onnx.is_file() and cfg.is_file():
        return onnx, cfg
    return None


def _piper_executable() -> Path | None:
    raw = (os.environ.get("HERMES_PIPER_BIN") or "").strip()
    if raw:
        p = Path(raw)
        if p.is_file():
            return p
    bin_dir = Path((os.environ.get("HERMES_PIPER_BIN_DIR") or "").strip() or _DEFAULT_PIPER_BIN_DIR)
    if bin_dir.is_dir():
        for candidate in (bin_dir / "piper", *bin_dir.rglob("piper")):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    found = shutil.which("piper")
    return Path(found) if found else None


def _piper_subprocess_env(exe: Path) -> dict[str, str]:
    env = os.environ.copy()
    for lib_dir in (exe.parent / "lib", exe.parent):
        if (lib_dir / "libonnxruntime.so").is_file() or any(lib_dir.glob("lib*.so*")):
            prev = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = f"{lib_dir}:{prev}" if prev else str(lib_dir)
            break
    return env


def tts_status() -> dict[str, Any]:
    paths = piper_model_paths()
    exe = _piper_executable()
    return {
        "configured_engine": (os.environ.get("HERMES_TTS_ENGINE") or "piper").strip().lower(),
        "active_engine": _LAST_ENGINE or None,
        "piper_voice": os.environ.get("HERMES_PIPER_VOICE", "it_IT-paola-medium"),
        "piper_model_ready": paths is not None,
        "piper_binary": str(exe) if exe else None,
        "piper_ready": paths is not None and exe is not None,
        "piper_onnx": str(paths[0]) if paths else None,
        "espeak": _espeak_exe(),
    }


def _set_engine(name: str) -> None:
    global _LAST_ENGINE
    _LAST_ENGINE = name


def _resample_44100_mono(src: Path, dst: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-ar", "44100", "-ac", "1", str(dst)],
                check=True,
                timeout=20,
                capture_output=True,
            )
            return dst.is_file() and dst.stat().st_size > 100
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            _LOG.warning("ffmpeg resample failed: %s", exc)
    try:
        from pydub import AudioSegment

        audio = AudioSegment.from_wav(str(src))
        audio = audio.set_frame_rate(44100).set_channels(1)
        audio.export(str(dst), format="wav")
        return dst.is_file()
    except Exception as exc:
        _LOG.warning("pydub resample failed: %s", exc)
    try:
        if dst != src:
            shutil.copy2(src, dst)
        return dst.is_file()
    except OSError:
        return False


def synthesize_wav(text: str, *, out_path: Path | None = None) -> Path | None:
    text = (text or "").strip()
    if not text:
        return None
    if out_path is None:
        out_path = Path(tempfile.gettempdir()) / f"hermes_tts_{uuid.uuid4().hex[:10]}.wav"

    engine = (os.environ.get("HERMES_TTS_ENGINE") or "piper").strip().lower()

    if engine in ("fast", "auto"):
        t0 = time.perf_counter()
        wav = _tts_espeak(text, out_path)
        if wav:
            _LOG.info("TTS espeak %.2fs", time.perf_counter() - t0)
            return wav
        wav = _tts_gtts(text, out_path)
        if wav:
            _LOG.info("TTS gTTS fallback %.2fs", time.perf_counter() - t0)
            return wav
        _LOG.warning("fast TTS: espeak/gTTS falliti, provo Piper")
        return _tts_piper(text, out_path)

    if engine == "piper":
        wav = _tts_piper(text, out_path)
        if wav:
            return wav
        _LOG.error("Piper fallito — niente fallback silenzioso su gTTS/espeak")
        return None

    if engine in ("gtts", "google", "natural"):
        wav = _tts_gtts(text, out_path)
        if wav:
            return wav

    if engine in ("espeak", "fast"):
        wav = _tts_espeak(text, out_path)
        if wav:
            return wav
        return _tts_gtts(text, out_path)

    return None


def _tts_piper_cli(text: str, onnx: Path, cfg: Path, out_wav: Path) -> Path | None:
    exe = _piper_executable()
    if not exe:
        _LOG.warning("binario Piper mancante (esegui install_hermes_piper_binary.py)")
        return None
    tmp = out_wav.with_suffix(".piper.wav")
    cmd = [str(exe), "--model", str(onnx), "--config", str(cfg), "--output_file", str(tmp)]
    try:
        subprocess.run(
            cmd,
            input=text.encode("utf-8"),
            check=True,
            timeout=90,
            capture_output=True,
            env=_piper_subprocess_env(exe),
        )
        _set_engine("piper")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        err = getattr(exc, "stderr", b"") or b""
        _LOG.warning("piper cli failed: %s %s", exc, err.decode(errors="replace")[:300])
        return None
    if not tmp.is_file() or tmp.stat().st_size < 100:
        return None
    return _piper_finish_wav(tmp, out_wav)


def _piper_finish_wav(tmp: Path, out_wav: Path) -> Path | None:
    if os.environ.get("HERMES_PIPER_RESAMPLE", "0").lower() in {"1", "true", "yes"}:
        if _resample_44100_mono(tmp, out_wav):
            tmp.unlink(missing_ok=True)
            return out_wav
    tmp.rename(out_wav)
    return out_wav if out_wav.is_file() else None


def _tts_piper(text: str, out_wav: Path) -> Path | None:
    paths = piper_model_paths()
    if not paths:
        return None
    onnx, cfg = paths
    # Binario arm64 prima (NX non ha modulo python piper)
    wav = _tts_piper_cli(text, onnx, cfg, out_wav)
    if wav:
        return wav
    try:
        from piper import PiperVoice

        tmp = out_wav.with_suffix(".piper.wav")
        voice = PiperVoice.load(str(onnx), config_path=str(cfg))
        import wave

        with wave.open(str(tmp), "wb") as wf:
            for chunk in voice.synthesize(text):
                if hasattr(chunk, "audio_int16_bytes"):
                    wf.writeframes(chunk.audio_int16_bytes)
                else:
                    wf.writeframes(bytes(chunk))
        _set_engine("piper-python")
        return _piper_finish_wav(tmp, out_wav)
    except Exception as exc:
        _LOG.warning("piper python failed: %s", exc)
        return None


def _tts_espeak(text: str, out_wav: Path) -> Path | None:
    exe = _espeak_exe()
    if not exe:
        return None
    speed = int(os.environ.get("HERMES_ESPEAK_SPEED", "260"))
    voice = (os.environ.get("HERMES_ESPEAK_VOICE") or "it").strip()
    tmp = out_wav.with_suffix(".raw.wav")
    try:
        subprocess.run(
            [exe, "-v", voice, "-s", str(speed), "-w", str(tmp), text],
            check=True,
            timeout=30,
            capture_output=True,
        )
        _set_engine("espeak")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        _LOG.warning("espeak failed: %s", exc)
        return None
    if not tmp.is_file() or tmp.stat().st_size < 100:
        return None
    if os.environ.get("HERMES_SKIP_TTS_RESAMPLE", "1").lower() in {"1", "true", "yes"}:
        tmp.rename(out_wav)
        return out_wav
    if _resample_44100_mono(tmp, out_wav):
        tmp.unlink(missing_ok=True)
        return out_wav
    tmp.rename(out_wav)
    return out_wav if out_wav.is_file() else None


def _tts_gtts(text: str, out_wav: Path) -> Path | None:
    try:
        from pydub import AudioSegment
        from gtts import gTTS
    except ImportError as exc:
        _LOG.warning("gTTS/pydub unavailable: %s", exc)
        return None
    voice = os.environ.get("HERMES_TTS_VOICE", "it")
    lang = voice[:2] if len(voice) >= 2 else "it"
    tmp_mp3 = out_wav.with_suffix(".mp3")
    try:
        gTTS(text=text, lang=lang).save(str(tmp_mp3))
        audio = AudioSegment.from_mp3(str(tmp_mp3))
        audio = audio.set_frame_rate(44100).set_channels(1)
        audio.export(str(out_wav), format="wav")
        _set_engine("gtts")
        return out_wav if out_wav.is_file() else None
    except Exception as exc:
        _LOG.warning("gTTS failed: %s", exc)
        return None
    finally:
        tmp_mp3.unlink(missing_ok=True)

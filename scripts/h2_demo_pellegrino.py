#!/usr/bin/env python3
"""Full H2 demo: gentle left arm + TTS + Cyberpunk WAV."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from h2_common import (
    GentleArmConfig,
    REPO_ROOT,
    init_dds,
    run_gentle_left_arm,
)
from h2_wav_util import play_pcm_stream, read_wav

TTS_TEXT = "Pellegrino Casoria di Accenture, sto arrivando"
DEFAULT_WAV = REPO_ROOT / "data" / "audio" / "cyberpunk_meme.wav"
PELLEGRINO_WAV = REPO_ROOT / "data" / "audio" / "pellegrino_tts.wav"


def _tts(volume: int) -> bool:
    wav = PELLEGRINO_WAV.resolve()
    if not wav.is_file():
        print(f"[demo] missing {wav} — run generate_pellegrino_tts_wav.py", file=sys.stderr)
        return False
    pcm, rate, ch, ok = read_wav(str(wav))
    if not ok or rate != 16000 or ch != 1:
        print(f"[demo] bad pellegrino wav rate={rate} ch={ch}")
        return False
    from h2_common import ensure_sdk_path

    ensure_sdk_path()
    from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

    audio = AudioClient()
    audio.SetTimeout(15.0)
    audio.Init()
    audio.SetVolume(volume)
    print(f"[demo] Pellegrino WAV (~{len(pcm) / 32000:.1f}s)")
    play_pcm_stream(audio, pcm, stream_name="h2_pellegrino", sample_rate=rate, num_channels=ch)
    audio.PlayStop("h2_pellegrino")
    return True


def _play_wav(wav: Path, volume: int) -> bool:
    pcm, rate, ch, ok = read_wav(str(wav))
    if not ok or rate != 16000 or ch != 1:
        print(f"[demo] bad wav rate={rate} ch={ch}")
        return False
    from h2_common import ensure_sdk_path

    ensure_sdk_path()
    from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

    audio = AudioClient()
    audio.SetTimeout(15.0)
    audio.Init()
    audio.SetVolume(volume)
    play_pcm_stream(audio, pcm, stream_name="h2_demo", sample_rate=rate, num_channels=ch)
    audio.PlayStop("h2_demo")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="H2 Pellegrino demo")
    parser.add_argument("--iface", default=None)
    parser.add_argument("--dry-arm", action="store_true")
    parser.add_argument("--dry-audio", action="store_true")
    parser.add_argument("--skip-meme", action="store_true")
    parser.add_argument("--wav", type=Path, default=DEFAULT_WAV)
    parser.add_argument("--rise", type=float, default=8.0)
    parser.add_argument("--hold", type=float, default=2.0)
    parser.add_argument("--lower", type=float, default=8.0)
    parser.add_argument("--yes", action="store_true", help="skip Enter prompt")
    args = parser.parse_args()

    if not args.yes and not args.dry_arm:
        print(
            "Demo: braccio LENTO (rt/arm_sdk) + TTS + audio.\n"
            "Robot in piedi, area libera, operatore presente.\n"
            "Modalità locomozione NON viene rilasciata."
        )
        input("Enter per avviare...")

    iface = init_dds(args.iface)
    print(f"[demo] DDS iface={iface}")

    if not args.dry_arm:
        cfg = GentleArmConfig(rise_s=args.rise, hold_s=args.hold, lower_s=args.lower)
        print("[demo] braccio sinistro (lento)...")
        run_gentle_left_arm(cfg, on_phase=lambda p: print(f"  arm: {p}"))

    if not args.dry_audio:
        print("[demo] TTS...")
        if not _tts(volume=85):
            return 1

    if not args.skip_meme and not args.dry_audio:
        wav = args.wav.resolve()
        if not wav.is_file():
            print(f"[demo] meme wav missing: {wav}", file=sys.stderr)
            return 1
        print("[demo] Cyberpunk meme...")
        if not _play_wav(wav, volume=90):
            return 1

    print("[demo] completato")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

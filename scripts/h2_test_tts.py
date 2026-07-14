#!/usr/bin/env python3
"""Step 1: play Pellegrino phrase on H2 speakers (Italian WAV — TtsMaker is Chinese-only)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from h2_common import REPO_ROOT, ensure_sdk_path, init_dds
from h2_wav_util import play_pcm_stream, read_wav

TTS_TEXT = "Pellegrino Casoria di Accenture, sto arrivando"
DEFAULT_WAV = REPO_ROOT / "data" / "audio" / "pellegrino_tts.wav"


def main() -> int:
    parser = argparse.ArgumentParser(description="H2 Pellegrino phrase (Italian WAV)")
    parser.add_argument("--iface", default=None, help="DDS interface (default eth10)")
    parser.add_argument("--wav", type=Path, default=DEFAULT_WAV)
    parser.add_argument("--volume", type=int, default=85)
    parser.add_argument(
        "--use-api",
        action="store_true",
        help="Use Unitree TtsMaker (Chinese only; Italian usually silent)",
    )
    parser.add_argument("--text", default=TTS_TEXT)
    args = parser.parse_args()

    iface = init_dds(args.iface)
    print(f"[h2] DDS on {iface}")

    ensure_sdk_path()
    from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

    audio = AudioClient()
    audio.SetTimeout(15.0)
    audio.Init()
    audio.SetVolume(args.volume)

    if args.use_api:
        import time

        code = audio.TtsMaker(args.text, 0)
        print(f"[h2] TtsMaker code={code} text={args.text!r}")
        if code != 0:
            return 1
        time.sleep(8.0)
        return 0

    wav = args.wav.expanduser().resolve()
    if not wav.is_file():
        print(
            f"[h2] missing {wav}\n"
            "  python scripts/generate_pellegrino_tts_wav.py",
            file=sys.stderr,
        )
        return 1

    pcm, rate, ch, ok = read_wav(str(wav))
    if not ok or rate != 16000 or ch != 1:
        print(f"[h2] bad wav rate={rate} ch={ch}", file=sys.stderr)
        return 1
    print(f"[h2] play {wav.name} (~{len(pcm) / 32000:.1f}s)")
    play_pcm_stream(audio, pcm, stream_name="h2_pellegrino", sample_rate=rate, num_channels=ch)
    audio.PlayStop("h2_pellegrino")
    print("[h2] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Step 3: play Cyberpunk meme WAV on H2 speakers."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from h2_common import REPO_ROOT, init_dds
from h2_wav_util import play_pcm_stream, read_wav

DEFAULT_WAV = REPO_ROOT / "data" / "audio" / "cyberpunk_meme.wav"


def main() -> int:
    parser = argparse.ArgumentParser(description="H2 WAV playback")
    parser.add_argument("--iface", default=None)
    parser.add_argument("--wav", type=Path, default=DEFAULT_WAV)
    parser.add_argument("--volume", type=int, default=90)
    args = parser.parse_args()

    wav = args.wav.expanduser().resolve()
    if not wav.is_file():
        print(f"[h2] WAV missing: {wav}", file=sys.stderr)
        return 1

    pcm, rate, ch, ok = read_wav(str(wav))
    if not ok or rate != 16000 or ch != 1:
        print(f"[h2] WAV must be 16kHz mono PCM16 (got rate={rate} ch={ch})", file=sys.stderr)
        return 1
    print(f"[h2] WAV {wav.name} bytes={len(pcm)} (~{len(pcm) / 32000:.1f}s)")

    iface = init_dds(args.iface)
    print(f"[h2] DDS on {iface}")

    from h2_common import ensure_sdk_path

    ensure_sdk_path()
    from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

    audio = AudioClient()
    audio.SetTimeout(15.0)
    audio.Init()
    audio.SetVolume(args.volume)
    play_pcm_stream(audio, pcm, stream_name="h2_cyberpunk", sample_rate=rate, num_channels=ch)
    audio.PlayStop("h2_cyberpunk")
    print("[h2] playback done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

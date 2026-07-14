#!/usr/bin/env python3
"""Generate Italian TTS WAV for H2 (Unitree TtsMaker is Chinese-only)."""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_VOICE = "it-IT-GiuseppeMultilingualNeural"
OUT_WAV = REPO / "data" / "audio" / "pellegrino_tts.wav"
WORK = REPO / "data" / "audio" / "_tts_parts"

# Accenchiùr / en clip per "Accenture" — evita pronuncia italiana errata
SEGMENTS = [
    ("it-IT-GiuseppeMultilingualNeural", "Pellegrino Casoria di "),
    ("en-US-GuyNeural", "Accenture"),
    ("it-IT-GiuseppeMultilingualNeural", ", sto arrivando"),
]


async def _save(voice: str, text: str, path: Path) -> None:
    import edge_tts

    await edge_tts.Communicate(text, voice).save(str(path))


async def _gen_all(parts: list[tuple[str, str]], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, (voice, text) in enumerate(parts):
        p = out_dir / f"part{i}.mp3"
        print(f"[tts-gen] {voice!r}: {text!r}")
        await _save(voice, text, p)
        paths.append(p)
    return paths


def _concat_mp3(parts: list[Path], out_mp3: Path) -> None:
    lst = out_mp3.parent / "concat_list.txt"
    lines = [f"file '{p.resolve().as_posix()}'" for p in parts]
    lst.write_text("\n".join(lines), encoding="utf-8")
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out_mp3)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=OUT_WAV)
    args = parser.parse_args()

    out_wav = args.out.resolve()
    mp3 = out_wav.with_suffix(".mp3")
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    paths = asyncio.run(_gen_all(SEGMENTS, WORK))
    _concat_mp3(paths, mp3)

    print(f"[tts-gen] ffmpeg -> {out_wav}")
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp3), "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", str(out_wav)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        return 1
    print(f"[tts-gen] OK {out_wav.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

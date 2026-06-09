#!/usr/bin/env python3
"""Genera WAV precaricati per Hermes (gTTS → 44.1kHz mono). Esegui prima del deploy NX."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from go2_dashboard.hermes.phrases import CANNED_KEYS  # noqa: E402
from go2_dashboard.hermes.speech import phrase_text  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "go2_dashboard" / "hermes" / "canned"


def _tts_wav(text: str, out_wav: Path) -> None:
    from go2_dashboard.hermes.tts_local import synthesize_wav

    if not synthesize_wav(text, out_path=out_wav):
        raise RuntimeError(f"TTS failed for: {text[:40]!r}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for key in CANNED_KEYS:
        text = phrase_text(key)
        dest = OUT / f"{key}.wav"
        print(f"{key}: {text!r} -> {dest}")
        _tts_wav(text, dest)
    print(f"OK {len(CANNED_KEYS)} files in {OUT}")


if __name__ == "__main__":
    main()

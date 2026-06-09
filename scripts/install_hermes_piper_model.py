#!/usr/bin/env python3
"""Scarica modello Piper italiano per Hermes (voce neurale, diversa da espeak/gTTS)."""

from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DIR = REPO / "go2_dashboard" / "hermes" / "piper"

# Voci italiane Piper (rhasspy)
VOICES = {
    "it_IT-paola-medium": (
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/it/it_IT/paola/medium/it_IT-paola-medium.onnx",
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/it/it_IT/paola/medium/it_IT-paola-medium.onnx.json",
    ),
    "it_IT-riccardo-x_low": (
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/it/it_IT/riccardo/x_low/it_IT-riccardo-x_low.onnx",
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/it/it_IT/riccardo/x_low/it_IT-riccardo-x_low.onnx.json",
    ),
}


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 1000:
        print(f"skip {dest.name} ({dest.stat().st_size} B)")
        return
    print(f"download {dest.name} …")
    urllib.request.urlretrieve(url, dest)
    print(f"  -> {dest} ({dest.stat().st_size} B)")


def main() -> None:
    voice = (os.environ.get("HERMES_PIPER_VOICE") or "it_IT-paola-medium").strip()
    urls = VOICES.get(voice)
    if not urls:
        print(f"Voce sconosciuta: {voice}. Disponibili: {', '.join(VOICES)}", file=sys.stderr)
        raise SystemExit(2)
    out_dir = Path(os.environ.get("HERMES_PIPER_DIR") or DEFAULT_DIR)
    _download(urls[0], out_dir / f"{voice}.onnx")
    _download(urls[1], out_dir / f"{voice}.onnx.json")
    print(f"OK piper model in {out_dir}")


if __name__ == "__main__":
    main()

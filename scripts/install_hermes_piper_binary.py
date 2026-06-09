#!/usr/bin/env python3
"""Installa binario Piper arm64 (pip piper-tts non funziona su Python 3.8 NX)."""

from __future__ import annotations

import os
import stat
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_BIN_DIR = REPO / "bin" / "piper"

# Release ufficiale rhasspy — aarch64 / Jetson
PIPER_TAR_URL = os.environ.get(
    "HERMES_PIPER_BINARY_URL",
    "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_aarch64.tar.gz",
)


def _find_piper_exe(root: Path) -> Path | None:
    for p in root.rglob("piper"):
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def main() -> None:
    dest_root = Path(os.environ.get("HERMES_PIPER_BIN_DIR") or DEFAULT_BIN_DIR)
    dest_root.mkdir(parents=True, exist_ok=True)
    exe = _find_piper_exe(dest_root)
    if exe:
        print(f"OK piper già presente: {exe}")
        return

    print(f"Scarico {PIPER_TAR_URL} …")
    with tempfile.TemporaryDirectory() as td:
        tgz = Path(td) / "piper.tar.gz"
        urllib.request.urlretrieve(PIPER_TAR_URL, tgz)
        with tarfile.open(tgz, "r:gz") as tf:
            tf.extractall(dest_root)
        exe = _find_piper_exe(dest_root)
        if not exe:
            raise SystemExit(f"piper non trovato dopo extract in {dest_root}")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"OK piper -> {exe}")


if __name__ == "__main__":
    main()

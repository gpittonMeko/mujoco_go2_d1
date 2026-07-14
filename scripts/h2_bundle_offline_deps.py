#!/usr/bin/env python3
"""Download offline Python deps for Jetson H2 demo (PC has internet, Jetson may not)."""
from __future__ import annotations

import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / ".cache" / "h2_offline"
URL = "https://files.pythonhosted.org/packages/62/88/4b440f5916976234838989c3214222a5abc6fd48009a992c4dc22a86c04f/cyclonedds-0.10.2.tar.gz"
DEST = OUT / "cyclonedds-0.10.2.tar.gz"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if DEST.is_file() and DEST.stat().st_size > 100_000:
        print(f"Already present: {DEST}")
        return 0
    print(f"Downloading cyclonedds 0.10.2 sdist -> {DEST}")
    urllib.request.urlretrieve(URL, DEST)
    print(f"OK ({DEST.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Una sola chiamata ``sport_accompany`` — pensato per ``subprocess.run`` dalla dashboard
(evita che un segfault CycloneDDS uccida il processo Flask).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from go2_accompany import sport_accompany


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True)
    ap.add_argument("--enable", default="1")
    ap.add_argument("--stand-up-first", dest="stand_first", default="0")
    ap.add_argument("--speed-level", dest="speed_level", default="")
    args = ap.parse_args()
    iface = (os.environ.get("GO2_DDS_INTERFACE") or "").strip() or None
    domain = int(os.environ.get("GO2_DDS_DOMAIN", "0"))
    sl: int | None = None
    if str(args.speed_level).strip() != "":
        sl = int(args.speed_level)
    out = sport_accompany(
        project_root=ROOT,
        domain=domain,
        iface=iface,
        enable=str(args.enable).lower() in {"1", "true", "yes", "on"},
        mode=str(args.mode).strip().lower(),
        stand_up_first=str(args.stand_first).lower() in {"1", "true", "yes", "on"},
        speed_level=sl,
    )
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()

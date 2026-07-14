#!/usr/bin/env python3
"""Avvia la dashboard jog D1 (slider + SDK DDS).

Uso locale / NX:
  python scripts/serve_d1_jog_dashboard.py
  # http://0.0.0.0:5056/  (porta D1_JOG_PORT)

Prima compilazione sulla macchina con DDS (NX o controller D1):
  bash scripts/build_d1_sdk.sh

Env utili:
  D1_JOG_ENABLE_REAL_ARM=1   — invia comandi (default 1 se GO2_LOCAL=1)
  D1_JOG_MODE=0              — funcode 2 smoothing 10 Hz (doc SDK)
  D1_DDS_DOMAIN / GO2_DDS_DOMAIN
  D1_ARM_HOST                — solo etichetta UI (default 192.168.123.100)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from go2_dashboard.d1_jog import create_d1_jog_app


def main() -> None:
    if os.environ.get("GO2_LOCAL", "0").lower() in {"1", "true", "yes", "on"}:
        os.environ.setdefault("D1_JOG_ENABLE_REAL_ARM", "1")
        os.environ.setdefault("GO2_ENABLE_REAL_ARM", "1")
    port = int(os.environ.get("D1_JOG_PORT", "5056"))
    bind = os.environ.get("D1_JOG_BIND", "0.0.0.0")
    app = create_d1_jog_app()
    print(f"D1 jog dashboard http://{bind}:{port}/")
    app.run(host=bind, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()

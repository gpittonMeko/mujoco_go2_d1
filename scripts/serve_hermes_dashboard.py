#!/usr/bin/env python3
"""Avvia Hermes — solo chat su :5054 (sulla NX, non sul PC).

Deploy + avvio sulla Jetson:
  py -3 scripts/deploy_hermes_to_nx.py

Dal PC apri: http://192.168.123.18:5054/

Sulla NX usa scripts/nx_start_hermes.sh (operator locale :5052 per contesto).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from go2_dashboard.hermes import create_hermes_app


def main() -> None:
    os.environ.setdefault("HERMES_OPERATOR_URL", "http://127.0.0.1:5052")
    port = int(os.environ.get("HERMES_PORT", "5054"))
    bind = os.environ.get("HERMES_BIND", "0.0.0.0")
    app = create_hermes_app()
    print(f"Hermes http://{bind}:{port}/")
    app.run(host=bind, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()

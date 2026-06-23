#!/usr/bin/env python3
"""Server Flask telemetria motori Go2 (DDS ``rt/lowstate``).

Uso simulatore (altro terminale: ``unitree_mujoco.py``)::

  set GO2_MOTOR_HEALTH_PORT=5054
  set GO2_DDS_DOMAIN=1
  set GO2_DDS_INTERFACE=lo
  python scripts/serve_go2_motor_health.py

Robot reale / Jetson NX::

  source scripts/nx_dashboard_env.sh   # oppure GO2_DDS_DOMAIN=0 GO2_DDS_INTERFACE=eth0
  python scripts/serve_go2_motor_health.py

Endpoint:
  GET /                  UI temperatura motori
  GET /api/health        smoke test
  GET /api/motor/state   snapshot JSON
  GET /api/motor/stream  SSE live

Soglie surriscaldamento (empiriche, override con env):
  GO2_MOTOR_TEMP_WARN=70
  GO2_MOTOR_TEMP_CRITICAL=85
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from go2_dashboard.motor_health_app import create_motor_health_app


def main() -> None:
    bind = os.environ.get("GO2_MOTOR_HEALTH_BIND", "0.0.0.0")
    port = int(os.environ.get("GO2_MOTOR_HEALTH_PORT", "5054"))
    app = create_motor_health_app()
    print(f"Go2 motor health  http://{bind}:{port}/")
    print(f"  DDS domain={os.environ.get('GO2_DDS_DOMAIN', '0')}  interface={os.environ.get('GO2_DDS_INTERFACE', '(default)')!r}")
    app.run(host=bind, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()

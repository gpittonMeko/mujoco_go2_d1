#!/usr/bin/env python3
"""Focused Go2 dashboard: Pick teach + base pose + motor health + Hermes (default :5056)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _warmup_cameras() -> None:
    if os.environ.get("GO2_LOCAL", "0").lower() not in {"1", "true", "yes", "on"}:
        return
    from go2_dashboard.cameras import CAMERA_CACHE

    CAMERA_CACHE.start()


def main() -> None:
    os.environ.setdefault("GO2_FOCUS_PORT", "5056")
    os.environ.setdefault("GO2_DASHBOARD_PORT", os.environ["GO2_FOCUS_PORT"])
    os.environ.setdefault("GO2_HERMES_INTEGRATED", "1")
    if os.environ.get("GO2_LOCAL", "0").lower() in {"1", "true", "yes", "on"}:
        os.environ.setdefault("GO2_CAMERA_CACHE_FPS", "10")
        os.environ.setdefault("GO2_MJPEG_FRAME_PERIOD_S", "0.10")
        os.environ.setdefault("GO2_ENABLE_REAL_ARM", "1")
    _warmup_cameras()

    from go2_dashboard.motor_health_env import apply_motor_health_env_defaults, ensure_thermal_settings_file

    apply_motor_health_env_defaults()
    ensure_thermal_settings_file()

    from go2_dashboard.focus_app import create_focus_app

    bind = os.environ.get("GO2_FOCUS_BIND", os.environ.get("GO2_DASHBOARD_BIND", "0.0.0.0")).strip()
    port = int(os.environ.get("GO2_FOCUS_PORT", "5056"))
    app = create_focus_app()
    print(f"go2_focus_dashboard http://{bind}:{port}/  pid={os.getpid()}")
    app.run(host=bind, port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()

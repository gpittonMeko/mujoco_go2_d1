#!/usr/bin/env python3
"""Dashboard operator (lite): porta default 5052 — **processo autonomo** (niente ``diagnostics_dashboard``)."""

from __future__ import annotations

import os
import sys


def _warmup_cameras() -> None:
    if os.environ.get("GO2_LOCAL", "0").lower() not in {"1", "true", "yes", "on"}:
        return
    from go2_dashboard.cameras import CAMERA_CACHE

    CAMERA_CACHE.start()


def main() -> None:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    os.environ.setdefault("GO2_DASHBOARD_PORT", "5052")

    if os.environ.get("GO2_LOCAL", "0").lower() in {"1", "true", "yes", "on"}:
        os.environ.setdefault("GO2_CAMERA_CACHE_FPS", "14")
        os.environ.setdefault("GO2_MJPEG_FRAME_PERIOD_S", "0.067")
        os.environ.setdefault("GO2_APRILTAG_MJPEG_PERIOD_S", "0.14")
        os.environ.setdefault("GO2_GRASP_EXECUTE_ARM", "1")

    _warmup_cameras()

    from go2_dashboard.lite_app import create_operators_app

    app = create_operators_app()
    host = os.environ.get("GO2_DASHBOARD_BIND", "0.0.0.0").strip()
    port = int(os.environ.get("GO2_DASHBOARD_PORT", "5052"))
    print(f"go2_operators_dashboard http://{host}:{port}/  pid={os.getpid()}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

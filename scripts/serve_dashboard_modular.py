#!/usr/bin/env python3
"""Avvio Flask dashboard. Default porta 5051 se ``GO2_DASHBOARD_PORT`` non è settata; NX/deploy usa 5050."""

from __future__ import annotations

import os
import sys
import threading


def main() -> None:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    os.environ.setdefault("GO2_DASHBOARD_PORT", "5051")

    # Sul robot: molte connessioni MJPEG + overlay AprilTag su tags.mjpg competono col GIL insieme a
    # /api/box/plan → chunk HTTP che si spezzano (ERR_INCOMPLETE_CHUNKED_ENCODING) e RPC Sport a singhiozzo.
    # Default più sobri solo se non hai già settato le variabili.
    _go2_local = os.environ.get("GO2_LOCAL", "0").lower() in {"1", "true", "yes"}
    if _go2_local:
        os.environ.setdefault("GO2_CAMERA_CACHE_FPS", "14")
        os.environ.setdefault("GO2_MJPEG_FRAME_PERIOD_S", "0.067")
        os.environ.setdefault("GO2_APRILTAG_MJPEG_PERIOD_S", "0.14")
        # Se non hai sourcato scripts/nx_dashboard_env.sh: default coerenti col deploy (non sovrascrivono env già settati).
        os.environ.setdefault("GO2_GRASP_EXECUTE_ARM", "1")
        os.environ.setdefault("GO2_GRASP_USE_FUSED_PLAN_IK", "1")
        os.environ.setdefault("GO2_GRASP_FUSED_WITH_CENTER", "1")
        os.environ.setdefault("GO2_GRASP_START_FOLD", "1")
        os.environ.setdefault("GO2_GRASP_GOTO_SAVED_START", "1")
        os.environ.setdefault("GO2_FRONT_CAMERA_FALLBACK_GRASP", "1")

    # Carica CAMERA_CACHE / legacy dopo aver eventualmente applicato GO2_CAMERA_CACHE_FPS.
    import diagnostics_dashboard as dd

    dd.warmup_realtime_feeds()

    if os.environ.get("GO2_MODULAR_BACKGROUND_DIAG", "0").lower() in {"1", "true", "yes"}:
        threading.Thread(target=dd.background_run, daemon=True).start()

    from go2_dashboard.app import create_modular_app

    app = create_modular_app(import_legacy_first=False)
    host = dd.GO2_DASHBOARD_BIND
    port = int(os.environ.get("GO2_DASHBOARD_PORT", "5051"))
    print(f"go2_dashboard http://{host}:{port}/  pid={os.getpid()}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

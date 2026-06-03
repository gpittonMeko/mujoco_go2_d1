"""Flask — streaming MJPEG camera Intel sulla NX (non Go2 integrata)."""

from __future__ import annotations

import os
import time
from typing import Any

from flask import Flask, Response, jsonify, render_template

from go2_dashboard.cameras import (
    CAMERA_DEVICES,
    CAMERA_CACHE,
    cv2,
    _v4l_index_for_logical_camera,
    usb_auto_v4l_mapping,
)
from go2_dashboard.paths import PROJECT_ROOT, VISION_WORKSPACE

INTEL_LOGICAL_DEVICE = int(os.environ.get("VISION_CAMERA_LOGICAL", "6"))
INTEL_LABEL = CAMERA_DEVICES.get(INTEL_LOGICAL_DEVICE, "Intel camera")


def _intel_allowed() -> bool:
    return INTEL_LOGICAL_DEVICE in CAMERA_DEVICES


def create_vision_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )
    _started = False

    def _ensure_cache() -> None:
        nonlocal _started
        if _started or not _intel_allowed():
            return
        CAMERA_CACHE.start(INTEL_LOGICAL_DEVICE)
        _started = True

    @app.route("/")
    def index() -> str:
        return render_template(
            "vision_dashboard.html",
            camera_logical=INTEL_LOGICAL_DEVICE,
            camera_label=INTEL_LABEL,
            vision_workspace=str(VISION_WORKSPACE.name),
            dashboard_port=int(os.environ.get("VISION_PORT", "5054")),
        )

    @app.route("/api/health")
    def health() -> Response:
        _ensure_cache()
        st = CAMERA_CACHE.stats().get(str(INTEL_LOGICAL_DEVICE), {})
        return jsonify(
            {
                "ok": True,
                "service": "vision_dashboard",
                "workspace": str(VISION_WORKSPACE.name),
                "camera_logical": INTEL_LOGICAL_DEVICE,
                "camera_label": INTEL_LABEL,
                "camera_available": bool(st.get("available")),
                "camera_error": st.get("error"),
                "opencv": cv2 is not None,
            }
        )

    @app.route("/api/camera/status")
    def camera_status() -> Response:
        _ensure_cache()
        v4l = _v4l_index_for_logical_camera(INTEL_LOGICAL_DEVICE) if _intel_allowed() else None
        auto = usb_auto_v4l_mapping()
        st = CAMERA_CACHE.stats()
        return jsonify(
            {
                "ok": True,
                "logical": INTEL_LOGICAL_DEVICE,
                "label": INTEL_LABEL,
                "v4l_index": v4l,
                "v4l_path": f"/dev/video{v4l}" if v4l is not None else None,
                "usb_auto_map": {str(k): v for k, v in auto.items()},
                "stats": st.get(str(INTEL_LOGICAL_DEVICE)),
            }
        )

    @app.route("/api/camera/snapshot.jpg")
    def snapshot_jpg() -> Response:
        _ensure_cache()
        jpg = CAMERA_CACHE.get_jpeg(INTEL_LOGICAL_DEVICE, wait_s=2.5)
        if jpg is None:
            return Response("no frame", status=503)
        return Response(jpg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.route("/stream/camera.mjpg")
    def stream_mjpg() -> Response:
        _ensure_cache()
        period = float(os.environ.get("VISION_MJPEG_PERIOD_S", "0.05"))

        def generate():
            last: bytes | None = None
            while True:
                jpg = CAMERA_CACHE.peek_jpeg(INTEL_LOGICAL_DEVICE)
                if jpg is None and last is None:
                    jpg = CAMERA_CACHE.get_jpeg(INTEL_LOGICAL_DEVICE, wait_s=2.0)
                if jpg is None:
                    jpg = last
                if jpg is not None:
                    last = jpg
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Cache-Control: no-store\r\n\r\n" + jpg + b"\r\n"
                    )
                time.sleep(period)

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app

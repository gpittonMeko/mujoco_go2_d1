"""Pagina Vision (Intel RealSense) — registrata sulla dashboard D1 :5053."""

from __future__ import annotations

import logging
import os
import time
import traceback
from collections.abc import Callable
from typing import Any

_PLACEHOLDER_MJPEG: bytes | None = None

from flask import Flask, Response, jsonify, render_template

from go2_dashboard.d1_jog import vision_detect
from go2_dashboard.d1_jog import vision_streams
from go2_dashboard.paths import VISION_WORKSPACE

_log = logging.getLogger(__name__)

INTEL_LOGICAL = int(os.environ.get("VISION_CAMERA_LOGICAL", "6"))
INTEL_LABEL_DEFAULT = "Intel RealSense D435i RGB stream"


def _load_cameras() -> tuple[Any | None, str | None]:
    try:
        from go2_dashboard import cameras as mod

        return mod, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def register_vision_routes(app: Flask, page_ctx: Callable[[], dict[str, Any]]) -> None:
    """Aggiunge /vision e API stream alla stessa app Flask del jog D1."""
    _cache_started = False
    _import_err: str | None = None
    cameras_mod, err = _load_cameras()
    if cameras_mod is None:
        _import_err = err

    def _ctx() -> dict[str, Any]:
        base = dict(page_ctx())
        base["dash_mode"] = "vision"
        base["vision_import_error"] = _import_err
        base["camera_logical"] = INTEL_LOGICAL
        if cameras_mod is not None:
            base["camera_label"] = cameras_mod.CAMERA_DEVICES.get(INTEL_LOGICAL, INTEL_LABEL_DEFAULT)
        else:
            base["camera_label"] = INTEL_LABEL_DEFAULT
        base["vision_workspace"] = VISION_WORKSPACE.name
        return base

    def _ensure_cache() -> None:
        nonlocal _cache_started
        if _cache_started or cameras_mod is None:
            return
        if INTEL_LOGICAL not in cameras_mod.CAMERA_DEVICES:
            return
        if os.environ.get("GO2_LOCAL", "0").lower() not in {"1", "true", "yes", "on"}:
            return
        try:
            from go2_dashboard import realsense_pyrs as rp

            rp.stop()
        except Exception:
            pass
        cameras_mod.CAMERA_CACHE.start(INTEL_LOGICAL)
        _cache_started = True

    def _placeholder_mjpeg(cv2: Any) -> bytes:
        global _PLACEHOLDER_MJPEG
        if _PLACEHOLDER_MJPEG:
            return _PLACEHOLDER_MJPEG
        img = vision_streams.placeholder_bgr(
            cv2,
            title="Camera in avvio",
            subtitle="attesa RealSense…",
        )
        enc = vision_streams.encode_jpeg(img, cv2) or b""
        _PLACEHOLDER_MJPEG = enc
        return enc

    @app.route("/vision")
    def vision_page() -> str | tuple[str, int]:
        try:
            return render_template("vision_dashboard.html", **_ctx())
        except Exception as exc:
            _log.exception("vision page render failed")
            return (
                f"<h1>Vision</h1><p>Errore pagina: {exc}</p>"
                f"<p><a href='/'>Torna al braccio</a></p>"
                f"<pre>{traceback.format_exc()}</pre>",
                500,
            )

    @app.route("/api/vision/health")
    def vision_health() -> Response:
        if cameras_mod is None:
            return jsonify(
                {
                    "ok": False,
                    "camera_logical": INTEL_LOGICAL,
                    "import_error": _import_err,
                    "opencv": False,
                    "go2_local": os.environ.get("GO2_LOCAL", "0"),
                }
            )
        _ensure_cache()
        cv2 = cameras_mod.cv2
        st = cameras_mod.CAMERA_CACHE.stats().get(str(INTEL_LOGICAL), {})
        return jsonify(
            {
                "ok": True,
                "camera_logical": INTEL_LOGICAL,
                "camera_label": cameras_mod.CAMERA_DEVICES.get(INTEL_LOGICAL),
                "camera_available": bool(st.get("available")),
                "camera_error": st.get("error") or _import_err,
                "opencv": cv2 is not None,
                "go2_local": os.environ.get("GO2_LOCAL", "0"),
            }
        )

    @app.route("/api/vision/camera/status")
    def vision_camera_status() -> Response:
        if cameras_mod is None:
            return jsonify({"ok": False, "import_error": _import_err}), 503
        try:
            _ensure_cache()
            v4l = (
                cameras_mod._v4l_index_for_logical_camera(INTEL_LOGICAL)
                if INTEL_LOGICAL in cameras_mod.CAMERA_DEVICES
                else None
            )
            return jsonify(
                {
                    "ok": True,
                    "logical": INTEL_LOGICAL,
                    "label": cameras_mod.CAMERA_DEVICES.get(INTEL_LOGICAL),
                    "v4l_index": v4l,
                    "v4l_path": f"/dev/video{v4l}" if v4l is not None else None,
                    "usb_auto_map": {str(k): v for k, v in cameras_mod.usb_auto_v4l_mapping().items()},
                    "stats": cameras_mod.CAMERA_CACHE.stats().get(str(INTEL_LOGICAL)),
                    "color_backend": os.environ.get("GO2_REALSENSE_COLOR_BACKEND", "auto"),
                    "detect_backend": os.environ.get("VISION_DETECT_BACKEND", "fusion"),
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "error_type": type(exc).__name__}), 500

    @app.route("/api/vision/streams/status")
    def vision_streams_status() -> Response:
        try:
            from go2_dashboard import realsense_pyrs as rp

            st = rp.status()
            peek = rp.peek_bundle()
            return jsonify(
                {
                    "ok": True,
                    "pyrs": st,
                    "has_bundle": peek is not None,
                    "streams": (peek or {}).get("streams") or st.get("streams"),
                    "detect_backend": os.environ.get("VISION_DETECT_BACKEND", "fusion"),
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/api/vision/camera/snapshot.jpg")
    def vision_snapshot() -> Response:
        if cameras_mod is None:
            return Response("cameras module unavailable", status=503)
        _ensure_cache()
        jpg = cameras_mod.CAMERA_CACHE.get_jpeg(INTEL_LOGICAL, wait_s=2.5)
        if jpg is None:
            return Response("no frame", status=503)
        return Response(jpg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.route("/api/vision/detector/status")
    def vision_detector_status() -> Response:
        try:
            return jsonify(vision_detect.detector_stack_status())
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/api/vision/detect/plan")
    def vision_detect_plan() -> Response:
        if cameras_mod is None:
            return jsonify({"ok": False, "import_error": _import_err}), 503
        cv2 = cameras_mod.cv2
        if cv2 is None:
            return jsonify({"ok": False, "error": "opencv_missing"}), 503
        _ensure_cache()
        jpg = cameras_mod.CAMERA_CACHE.get_jpeg(INTEL_LOGICAL, wait_s=2.5)
        if jpg is None:
            return jsonify({"ok": False, "error": "no_camera_frame"}), 503
        try:
            plan = vision_detect.run_plan_on_jpeg(jpg, cv2, logical_camera=INTEL_LOGICAL)
            return jsonify(vision_detect.plan_summary(plan))
        except Exception as exc:
            _log.exception("vision detect plan")
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/api/vision/detect/last")
    def vision_detect_last() -> Response:
        return jsonify(vision_detect.last_plan_summary())

    @app.route("/api/vision/detect/calibrate", methods=["POST"])
    def vision_detect_calibrate() -> Response:
        if cameras_mod is None:
            return jsonify({"ok": False, "error": "no_camera"}), 503
        cv2 = cameras_mod.cv2
        if cv2 is None:
            return jsonify({"ok": False, "error": "no_opencv"}), 503
        _ensure_cache()
        jpg = cameras_mod.CAMERA_CACHE.get_jpeg(INTEL_LOGICAL, wait_s=2.5)
        if jpg is None:
            return jsonify({"ok": False, "error": "no_frame"}), 503
        frame = vision_detect._frame_from_jpeg(jpg, cv2)
        if frame is None:
            return jsonify({"ok": False, "error": "decode_fail"}), 503
        return jsonify(vision_detect.calibrate_background(frame))

    @app.route("/stream/vision/detect.mjpg")
    def vision_stream_detect_mjpg() -> Response:
        """MJPEG con bbox YOLO/fallback + punto presa + preview IK."""
        if cameras_mod is None:
            return Response("cameras unavailable", status=503)
        cv2 = cameras_mod.cv2
        if cv2 is None:
            return Response("opencv unavailable", status=503)
        _ensure_cache()
        period = float(os.environ.get("VISION_DETECT_MJPEG_PERIOD_S", "0.08"))
        every_n = max(1, int(os.environ.get("VISION_DETECT_EVERY_N_FRAMES", "4")))
        ph = _placeholder_mjpeg(cv2)

        def generate():
            last_out: bytes | None = ph or None
            last_raw: bytes | None = None
            frame_i = 0
            while True:
                try:
                    jpg = cameras_mod.CAMERA_CACHE.peek_jpeg(INTEL_LOGICAL)
                    if jpg is None and last_raw is None:
                        jpg = cameras_mod.CAMERA_CACHE.get_jpeg(INTEL_LOGICAL, wait_s=2.0)
                    if jpg is not None:
                        last_raw = jpg
                    jpg = last_raw
                    if jpg is not None:
                        frame_i += 1
                        if frame_i % every_n == 0:
                            enc = vision_detect.encode_overlay_jpeg(
                                jpg, cv2, logical_camera=INTEL_LOGICAL
                            )
                            if enc is not None:
                                last_out = enc
                        if last_out is None or last_out is ph:
                            last_out = jpg
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Cache-Control: no-store\r\n\r\n" + (last_out or ph) + b"\r\n"
                    )
                except Exception:
                    _log.exception("vision detect mjpeg")
                time.sleep(period)

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.route("/stream/vision/camera.mjpg")
    def vision_stream_mjpg() -> Response:
        if cameras_mod is None:
            return Response("cameras unavailable", status=503)

        cv2 = cameras_mod.cv2
        if cv2 is None:
            return Response("opencv unavailable", status=503)
        _ensure_cache()
        period = float(os.environ.get("VISION_MJPEG_PERIOD_S", "0.05"))
        ph = _placeholder_mjpeg(cv2)

        def generate():
            last: bytes | None = ph or None
            while True:
                try:
                    jpg = cameras_mod.CAMERA_CACHE.peek_jpeg(INTEL_LOGICAL)
                    if jpg is None and last is ph:
                        jpg = cameras_mod.CAMERA_CACHE.get_jpeg(INTEL_LOGICAL, wait_s=2.0)
                    if jpg is not None:
                        last = jpg
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Cache-Control: no-store\r\n\r\n" + (last or ph) + b"\r\n"
                    )
                except Exception:
                    _log.exception("vision mjpeg frame")
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

    def _stream_realsense_panel(panel: str) -> Response:
        """MJPEG per uscita camera: color, depth, ir1, ir2, grid."""
        if cameras_mod is None:
            return Response("cameras unavailable", status=503)
        cv2 = cameras_mod.cv2
        if cv2 is None:
            return Response("opencv unavailable", status=503)
        _ensure_cache()
        period = float(os.environ.get("VISION_STREAM_MJPEG_PERIOD_S", "0.08"))

        def generate():
            from go2_dashboard import realsense_pyrs as rp

            ph = _placeholder_mjpeg(cv2)
            last_out: bytes | None = ph or None
            while True:
                try:
                    if panel == "color":
                        jpg = cameras_mod.CAMERA_CACHE.peek_jpeg(INTEL_LOGICAL)
                        if jpg is None:
                            jpg = cameras_mod.CAMERA_CACHE.get_jpeg(INTEL_LOGICAL, wait_s=0.5)
                        if jpg:
                            last_out = jpg
                    else:
                        peek = rp.peek_bundle()
                        previews = vision_streams.bundle_preview_jpegs(peek, cv2)
                        jpg = previews.get(panel)
                        if jpg:
                            last_out = jpg
                    out = last_out or ph
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Cache-Control: no-store\r\n\r\n" + out + b"\r\n"
                    )
                except Exception:
                    _log.exception("vision stream %s", panel)
                time.sleep(period)

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.route("/stream/vision/color.mjpg")
    def vision_stream_color_mjpg() -> Response:
        return _stream_realsense_panel("color")

    @app.route("/stream/vision/depth.mjpg")
    def vision_stream_depth_mjpg() -> Response:
        return _stream_realsense_panel("depth")

    @app.route("/stream/vision/ir.mjpg")
    def vision_stream_ir_mjpg() -> Response:
        return _stream_realsense_panel("ir1")

    @app.route("/stream/vision/ir1.mjpg")
    def vision_stream_ir1_mjpg() -> Response:
        return _stream_realsense_panel("ir1")

    @app.route("/stream/vision/ir2.mjpg")
    def vision_stream_ir2_mjpg() -> Response:
        return _stream_realsense_panel("ir2")

    @app.route("/stream/vision/grid.mjpg")
    def vision_stream_grid_mjpg() -> Response:
        return _stream_realsense_panel("grid")

    @app.route("/stream/vision/yolo.mjpg")
    def vision_stream_yolo_mjpg() -> Response:
        """RGB con tutte le bbox YOLO disegnate."""
        if cameras_mod is None:
            return Response("cameras unavailable", status=503)
        cv2 = cameras_mod.cv2
        if cv2 is None:
            return Response("opencv unavailable", status=503)
        _ensure_cache()
        period = float(os.environ.get("VISION_DETECT_MJPEG_PERIOD_S", "0.1"))
        every_n = max(1, int(os.environ.get("VISION_DETECT_EVERY_N_FRAMES", "2")))

        ph = _placeholder_mjpeg(cv2)

        def generate():
            last_out: bytes | None = ph or None
            frame_i = 0
            while True:
                try:
                    jpg = cameras_mod.CAMERA_CACHE.peek_jpeg(INTEL_LOGICAL)
                    if jpg is None:
                        jpg = cameras_mod.CAMERA_CACHE.get_jpeg(INTEL_LOGICAL, wait_s=1.5)
                    if jpg is not None:
                        frame_i += 1
                        if frame_i % every_n == 0:
                            enc = vision_detect.encode_overlay_jpeg(
                                jpg, cv2, logical_camera=INTEL_LOGICAL
                            )
                            if enc is not None:
                                last_out = enc
                        if last_out is None or last_out is ph:
                            last_out = jpg
                    out = last_out or ph
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Cache-Control: no-store\r\n\r\n" + out + b"\r\n"
                    )
                except Exception:
                    _log.exception("vision yolo mjpeg")
                time.sleep(period)

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

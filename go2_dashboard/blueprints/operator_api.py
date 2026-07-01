"""Route HTTP dedicate alla dashboard operator (nessun mount da ``diagnostics_dashboard``)."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from flask import Blueprint, Response, jsonify, request

from go2_dashboard.cameras import (
    CAMERA_CACHE,
    CAMERA_DEVICES,
    _v4l_index_for_logical_camera,
    usb_auto_v4l_mapping,
)
from go2_dashboard.operator_scene import build_grasp_pipeline_stub, build_scene_3d_payload
from go2_dashboard.operator_stack import go2_local, nx_stack_status
from go2_dashboard.paths import PROJECT_ROOT
from go2_dashboard.sport_lane import accompany_mode_handle, sport_last_payload

bp = Blueprint("go2_operator_api", __name__)

_PROCESS_STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
START_ALIGNMENT_PATH = PROJECT_ROOT / "data" / "start_alignment.json"

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


def _depth_v4l_index_for_logical_camera(device: int) -> int | None:
    key = f"GO2_DEPTH_VIDEO_INDEX_{int(device)}"
    raw = os.environ.get(key, os.environ.get("GO2_DEPTH_VIDEO_INDEX", "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@bp.route("/api/health", methods=["GET"])
def api_health() -> Any:
    return jsonify(
        {
            "ok": True,
            "service": "go2_dashboard",
            "pid": os.getpid(),
            "process_started_at": _PROCESS_STARTED_AT,
            "dashboard_py_mtime": None,
            "reload_recommended": False,
            "reload_hint": None,
            "operator_dashboard": True,
        }
    )


@bp.route("/api/status", methods=["GET"])
def api_status() -> Any:
    return jsonify({"ok": True, "operator_dashboard": True, "pid": os.getpid()})


@bp.route("/api/cameras/status", methods=["GET"])
def api_cameras_status() -> Any:
    if go2_local():
        CAMERA_CACHE.start()
    payload: dict[str, Any] = {
        "ok": True,
        "go2_local": go2_local(),
        "mode": "local-cache" if go2_local() else "ssh-snapshot",
        "cameras": CAMERA_CACHE.stats(),
    }
    if go2_local() and cv2 is not None:
        payload["v4l_index_by_logical"] = {str(d): _v4l_index_for_logical_camera(d) for d in CAMERA_DEVICES}
        depth_map = {str(d): _depth_v4l_index_for_logical_camera(d) for d in CAMERA_DEVICES}
        if any(v is not None for v in depth_map.values()):
            payload["depth_v4l_index_by_logical"] = depth_map
        auto_m = usb_auto_v4l_mapping()
        if auto_m:
            payload["v4l_usb_auto_map"] = {str(k): int(v) for k, v in sorted(auto_m.items())}
    return jsonify(payload)


def _robot_camera_jpeg(device: int) -> bytes | None:
    if go2_local() and cv2 is not None:
        return CAMERA_CACHE.get_jpeg(device)
    return None


@bp.route("/api/robot/camera/<int:device>.jpg")
def api_robot_camera_jpg(device: int) -> Any:
    if device not in CAMERA_DEVICES:
        return Response("camera not allowed", status=404)
    image = _robot_camera_jpeg(device)
    if image is None:
        return Response("camera frame unavailable", status=503)
    return Response(image, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@bp.route("/stream/robot/camera/<int:device>.mjpg")
def stream_robot_camera_mjpg(device: int) -> Any:
    if device not in CAMERA_DEVICES:
        return Response("camera not allowed", status=404)
    period = float(os.environ.get("GO2_MJPEG_FRAME_PERIOD_S", "0.05"))
    if not go2_local():
        period = max(period, 0.12)

    def generate():
        last: bytes | None = None
        first_wait_s = float(os.environ.get("GO2_MJPEG_FIRST_FRAME_WAIT_S", "1.8"))
        while True:
            if go2_local() and cv2 is not None:
                jpg = CAMERA_CACHE.peek_jpeg(device)
                if jpg is None and last is None:
                    jpg = CAMERA_CACHE.get_jpeg(device, wait_s=first_wait_s)
            else:
                jpg = _robot_camera_jpeg(device)
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
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@bp.route("/api/nx/stack/status", methods=["GET"])
def api_nx_stack_status() -> Any:
    return jsonify({"ok": True, **nx_stack_status()})


@bp.route("/api/nx/stack/start", methods=["POST"])
def api_nx_stack_start() -> Any:
    if not go2_local():
        return jsonify({"ok": False, "reason": "GO2_LOCAL!=1", **nx_stack_status()}), 400
    CAMERA_CACHE.start(0)
    CAMERA_CACHE.start(6)
    return jsonify({"ok": True, "message": "Camera cache avviata (0,6).", **nx_stack_status()})


@bp.route("/api/base/sport_last", methods=["GET"])
def api_base_sport_last() -> Any:
    return jsonify(sport_last_payload())


@bp.route("/api/base/accompany_mode", methods=["GET", "POST"])
def api_base_accompany_mode() -> Any:
    payload, code = accompany_mode_handle(request)
    return jsonify(payload), code


@bp.route("/api/alignment/start_pose", methods=["GET", "POST"])
def api_alignment_start_pose() -> Any:
    if request.method == "GET":
        if not START_ALIGNMENT_PATH.exists():
            return jsonify({"ok": False, "reason": "no_saved_start_pose"}), 404
        try:
            data = json.loads(START_ALIGNMENT_PATH.read_text(encoding="utf-8"))
            return jsonify({"ok": True, "path": str(START_ALIGNMENT_PATH), "start_pose": data})
        except Exception as exc:
            return jsonify({"ok": False, "reason": repr(exc)}), 500
    return (
        jsonify(
            {
                "ok": False,
                "reason": "POST_start_pose_requires_monolith_planner",
                "hint_it": "Salvare START con piano AprilTag richiede la dashboard monolite (``api_box_plan``). "
                "Sulla NX usa il processo sulla porta 5050 oppure estendi l'operator.",
            }
        ),
        501,
    )


@bp.route("/api/arm/scene_3d", methods=["GET"])
def api_arm_scene_3d() -> Any:
    fast = request.args.get("fast", "").strip().lower() in ("1", "true", "yes")
    resp = jsonify(build_scene_3d_payload(geometry_fast=fast))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@bp.route("/api/arm/grasp_pipeline", methods=["GET"])
def api_arm_grasp_pipeline() -> Any:
    resp = jsonify(build_grasp_pipeline_stub())
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

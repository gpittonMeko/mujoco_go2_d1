"""Flask app — pagina jog D1 con slider (SDK)."""

from __future__ import annotations

import os
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context
from werkzeug.exceptions import HTTPException

from go2_dashboard.d1_jog import (
    cartesian,
    grasp6d,
    jog_stream,
    orbbec_capture,
    pick_preset,
    pick_vision,
    program_runner,
    program_store,
    service,
    wrist_rgbd,
)
from go2_dashboard.paths import PROJECT_ROOT
from go2_dashboard.sport_lane import (
    GO2_DDS_DOMAIN,
    GO2_DDS_INTERFACE,
    accompany_mode_handle,
    base_motion_allowed,
    sport_last_payload,
)

THERMAL_SETTINGS: dict[str, Any] = {
    "warn_c": 62.0,
    "critical_c": 72.0,
    "auto_crouch": False,
}

_PROCESS_STARTED = datetime.now().isoformat(timespec="seconds")
_GO2_HERO_CANDIDATES = (
    PROJECT_ROOT / "data" / "unitree_robot_main.png",
)
_GO2_FEATURES_CANDIDATES = (
    PROJECT_ROOT / "data" / "unitree_robot_main.png",
)


def _go2_svg_fallback(*, label: str) -> bytes:
    svg = f"""
<svg xmlns='http://www.w3.org/2000/svg' width='1280' height='720' viewBox='0 0 1280 720'>
  <defs>
    <linearGradient id='bg' x1='0' y1='0' x2='0' y2='1'>
      <stop offset='0%' stop-color='#bfefff'/>
      <stop offset='100%' stop-color='#66c6ff'/>
    </linearGradient>
    <linearGradient id='ground' x1='0' y1='0' x2='0' y2='1'>
      <stop offset='0%' stop-color='#9ae58d'/>
      <stop offset='100%' stop-color='#49b95d'/>
    </linearGradient>
  </defs>
  <rect width='1280' height='720' fill='url(#bg)'/>
  <ellipse cx='1030' cy='118' rx='95' ry='95' fill='rgba(255,255,255,.65)'/>
  <rect y='560' width='1280' height='160' fill='url(#ground)'/>
  <g transform='translate(248 276)' fill='#0a5f90' stroke='#0a5f90' stroke-linecap='round'>
    <rect x='80' y='48' width='590' height='120' rx='46' fill='#1383c2'/>
    <rect x='622' y='58' width='110' height='72' rx='25' fill='#1383c2'/>
    <circle cx='704' cy='95' r='14' fill='#9fe4ff'/>
    <rect x='196' y='74' width='300' height='46' rx='20' fill='#c6f4ff' stroke='none'/>
    <path d='M145 164 102 286' stroke-width='34'/>
    <path d='M318 164 288 290' stroke-width='34'/>
    <path d='M494 164 532 290' stroke-width='34'/>
    <path d='M648 164 690 286' stroke-width='34'/>
    <path d='M736 86 796 50' stroke-width='23'/>
    <circle cx='811' cy='42' r='18'/>
  </g>
  <text x='58' y='90' fill='#0e5b86' font-size='52' font-family='Trebuchet MS, Segoe UI, sans-serif' font-weight='700'>Unitree Go2</text>
  <text x='60' y='136' fill='#1b6e99' font-size='30' font-family='Trebuchet MS, Segoe UI, sans-serif'>{label}</text>
</svg>
""".strip()
    return svg.encode("utf-8")


def _usb_vid_pid_for_v4l(index: int) -> tuple[str, str] | None:
    try:
        cur = Path(f"/sys/class/video4linux/video{int(index)}/device").resolve()
    except OSError:
        return None
    for _ in range(20):
        vendor = cur / "idVendor"
        product = cur / "idProduct"
        if vendor.is_file() and product.is_file():
            try:
                return vendor.read_text().strip().lower(), product.read_text().strip().lower()
            except OSError:
                return None
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _is_realsense_color_capture_node(index: int) -> bool:
    """RealSense RGB capture is UVC interface 1.3, stream index 0."""
    try:
        device_path = str(Path(f"/sys/class/video4linux/video{int(index)}/device").resolve())
        stream_index = int(Path(f"/sys/class/video4linux/video{int(index)}/index").read_text().strip())
    except (OSError, ValueError):
        return False
    return ":1.3" in device_path and stream_index == 0


def _resolve_realsense_rgb_index(*, role: str) -> int:
    if role == "wrist":
        env_key, pid, fallback = "D1_WRIST_V4L_INDEX", "0b5c", 4
    else:
        env_key, pid, fallback = "D1_FRONT_V4L_INDEX", "0b3a", 10
    candidates: list[int] = []
    base = Path("/sys/class/video4linux")
    if base.is_dir():
        for node in sorted(base.glob("video*")):
            tail = node.name[5:]
            if not tail.isdigit():
                continue
            idx = int(tail)
            pair = _usb_vid_pid_for_v4l(idx)
            if pair == ("8086", pid) and _is_realsense_color_capture_node(idx):
                candidates.append(idx)
    try:
        configured = int(os.environ.get(env_key, str(fallback)))
    except ValueError:
        configured = fallback
    if configured in candidates:
        return configured
    return candidates[0] if candidates else configured


def _frame_chroma_bgr(frame: Any) -> float:
    try:
        import cv2

        return float(
            cv2.mean(cv2.absdiff(frame[:, :, 0], frame[:, :, 1]))[0]
            + cv2.mean(cv2.absdiff(frame[:, :, 1], frame[:, :, 2]))[0]
        )
    except Exception:
        return 0.0


def _mount_motor_health(app: Flask) -> None:
    """Restore the complete historical motor-management UI inside port 5056."""
    from go2_dashboard.motor_health_app import create_motor_health_app

    motor_app = create_motor_health_app()
    for rule in motor_app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        path = "/motors" if rule.rule == "/" else rule.rule
        if rule.rule == "/api/health":
            path = "/api/motor/health"
        endpoint = f"motor_health.{rule.endpoint}"
        methods = sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})
        app.add_url_rule(path, endpoint, motor_app.view_functions[rule.endpoint], methods=methods)


def create_d1_jog_app() -> Flask:
    template_dir = PROJECT_ROOT / "templates"
    static_dir = PROJECT_ROOT / "static"
    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir) if static_dir.is_dir() else None,
        static_url_path="/static",
    )
    front_last_jpg: bytes | None = None
    wrist_last_jpg: bytes | None = None
    camera_diag: dict[str, dict[str, Any]] = {
        "wrist": {"index": None, "chroma": None, "rgb_like": False},
        "front": {"index": None, "chroma": None, "rgb_like": False},
    }
    startup_arm_stabilization: dict[str, Any] = {"ok": False, "reason": "not_run"}

    @app.after_request
    def _cors(resp: Response) -> Response:
        if request.path.startswith("/api/"):
            resp.headers.setdefault("Access-Control-Allow-Origin", "*")
            resp.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            resp.headers.setdefault("Access-Control-Allow-Headers", "Content-Type")
        return resp

    @app.errorhandler(Exception)
    def _api_json_error(exc: Exception) -> tuple[Response, int]:
        if not request.path.startswith("/api/"):
            if isinstance(exc, HTTPException):
                return exc
            raise exc
        return jsonify({"ok": False, "reason": str(exc), "error": type(exc).__name__}), 500

    @app.route("/api/<path:_sub>", methods=["OPTIONS"])
    def _options(_sub: str) -> Response:
        return Response(status=204)

    def _page_ctx(*, dash_mode: str = "arm") -> dict[str, str | int]:
        return {
            "dashboard_port": int(os.environ.get("D1_JOG_PORT", os.environ.get("GO2_DASHBOARD_PORT", "5056"))),
            "d1_arm_host": os.environ.get("D1_ARM_HOST", os.environ.get("SERVO_ARM_HOST", "192.168.123.100")),
            "go2_local": os.environ.get("GO2_LOCAL", "0"),
            "dash_mode": dash_mode,
        }

    @app.route("/")
    def index() -> str:
        return render_template("d1_jog_dashboard.html", **_page_ctx(dash_mode="arm"))

    @app.route("/focus/teach")
    def focus_teach_alias() -> str:
        return render_template("d1_jog_dashboard.html", **_page_ctx(dash_mode="arm"))

    @app.route("/focus/hermes")
    def focus_hermes() -> str:
        return render_template(
            "hermes.html",
            port=int(os.environ.get("D1_JOG_PORT", "5056")),
        )

    @app.route("/api/assets/go2_hero.png")
    @app.route("/assets/unitree_go2_hero.png")
    @app.route("/assets/unitree_robot_main.png")
    def go2_hero_asset() -> Response:
        for path in _GO2_HERO_CANDIDATES:
            if path.is_file():
                return send_file(path, mimetype="image/png", max_age=0)
        return Response(_go2_svg_fallback(label="Frutiger Aero hero card"), mimetype="image/svg+xml")

    @app.route("/api/assets/go2_features.png")
    @app.route("/assets/unitree_go2_features.png")
    def go2_features_asset() -> Response:
        for path in _GO2_FEATURES_CANDIDATES:
            if path.is_file():
                return send_file(path, mimetype="image/png", max_age=0)
        return Response(_go2_svg_fallback(label="Live dashboard visual"), mimetype="image/svg+xml")

    @app.route("/program")
    def program_editor() -> str:
        return render_template("d1_program_editor.html", **_page_ctx(dash_mode="arm"))

    @app.route("/api/motion/status")
    def motion_status() -> Response:
        return jsonify(service.motion_status())

    @app.route("/api/daemon/status")
    def daemon_status() -> Response:
        return jsonify({"ok": True, **service.runtime_safety_status()})

    @app.route("/api/motion/reset", methods=["POST"])
    def motion_reset() -> Response:
        return jsonify(service.motion_reset())

    @app.route("/api/health")
    def health() -> Response:
        st = service.binaries_status()
        daemon = st.get("command_daemon") or {}
        return jsonify(
            {
                "ok": (
                    st["command_ok"]
                    and st["feedback_ok"]
                    and bool(daemon.get("alive"))
                ),
                "service": "d1_jog_dashboard",
                "started_at": _PROCESS_STARTED,
                "binaries": st,
                "command_daemon": daemon,
                "runtime_safety": service.runtime_safety_status(),
                "startup_arm_stabilization": startup_arm_stabilization,
                "dds_domain": int(os.environ.get("D1_DDS_DOMAIN", os.environ.get("GO2_DDS_DOMAIN", "0"))),
                "dds_interface": (os.environ.get("GO2_DDS_INTERFACE") or os.environ.get("D1_DDS_INTERFACE") or "eth0"),
                "cyclonedds_uri_set": bool((os.environ.get("CYCLONEDDS_URI") or "").strip()),
                "dds_runtime_ok": os.environ.get("D1_DDS_RUNTIME_OK") == "1",
                "dds_runtime_dir": os.environ.get(
                    "D1_DDS_LIB_DIR", "/home/unitree/sdk_reinstall_backup_19700225_160102"
                ),
            }
        )

    def _parse_servo_env(key: str, default_vals: list[float]) -> list[float]:
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            return service.clamp_servo_deg(default_vals)
        try:
            vals = [float(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError:
            return service.clamp_servo_deg(default_vals)
        if len(vals) < 7:
            return service.clamp_servo_deg(default_vals)
        return service.clamp_servo_deg(vals[:7])

    def _scan_side_target(side: str) -> list[float]:
        # Il mapping fisico va verificato sul robot: i riferimenti storici erano invertiti
        # rispetto ai pulsanti UI, quindi li teniamo espliciti qui.
        left_default = [-90.0, 19.2, 26.0, 0.1, 37.8, 0.4, 5.0]
        right_default = [90.0, 19.2, 26.0, 0.1, 37.8, 0.4, 5.0]
        if side == "left":
            return _parse_servo_env("D1_SCAN_LEFT_DEG", left_default)
        return _parse_servo_env("D1_SCAN_RIGHT_DEG", right_default)

    def _safe_transit_target() -> list[float] | None:
        fb = service.read_servo_deg(fast=True)
        base = fb.get("servo_deg") if fb.get("ok") else service.get_servo_cache()
        if not isinstance(base, list) or len(base) < 7:
            return None
        return service.safe_zero_pose_from_servo([float(x) for x in base[:7]])

    def _move_via_safe_transit(target_servo_deg: list[float]) -> dict[str, Any]:
        program_runner.request_stop()
        service.motion_reset()
        program_runner.clear_stop_request()
        transit = _safe_transit_target()
        if transit is None:
            return {
                "ok": False,
                "reason": "safe_transit_unavailable",
                "safety_interlock": True,
                "target_servo_deg": target_servo_deg,
            }
        service._halt_cartesian_stream(wait_idle=True)
        couple = service.ensure_coupled_for_motion()
        if not couple.get("ok"):
            return {"ok": False, "reason": "couple_failed", "coupling": couple, "target_servo_deg": target_servo_deg}
        transit_move = None
        transit_move = _move_to_point_with_busy_recovery(transit)
        if not (transit_move.get("ok") or transit_move.get("skipped")):
            transit_move["target_servo_deg"] = transit
            transit_move["coupling"] = couple
            transit_move["phase"] = "fold_before_rotate"
            return transit_move
        side_transit = transit[:]
        side_transit[0] = float(target_servo_deg[0])
        side_transit_move = _move_to_point_with_busy_recovery(side_transit)
        if not (side_transit_move.get("ok") or side_transit_move.get("skipped")):
            side_transit_move["target_servo_deg"] = side_transit
            side_transit_move["coupling"] = couple
            side_transit_move["phase"] = "rotate_while_folded"
            return side_transit_move
        move = _move_to_point_with_busy_recovery(target_servo_deg)
        move["coupling"] = couple
        move["transit_zero"] = transit
        move["transit_move"] = transit_move
        move["side_transit"] = side_transit
        move["side_transit_move"] = side_transit_move
        move["target_servo_deg"] = target_servo_deg
        return move

    def _move_to_point_with_busy_recovery(target_servo_deg: list[float]) -> dict[str, Any]:
        out = program_runner.move_to_servo_deg_smooth(target_servo_deg)
        if out.get("reason") == "motion_busy:program":
            program_runner.request_stop()
            time.sleep(0.15)
            service.motion_reset()
            program_runner.clear_stop_request()
            out = program_runner.move_to_servo_deg_smooth(target_servo_deg)
            out["recovered_from_busy"] = True
        return out

    def _open_rgb_cap(cv2: Any, idx: int) -> Any:
        cap = cv2.VideoCapture(f"/dev/video{idx}", cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        return cap

    def _read_rgb_frame(cv2: Any, idx: int) -> Any | None:
        cap = _open_rgb_cap(cv2, idx)
        if not cap.isOpened():
            cap.release()
            return None
        best = None
        best_score = -1.0
        try:
            # Let auto-exposure settle: the first valid frame is often very dark.
            for _ in range(18):
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                chroma = _frame_chroma_bgr(frame)
                brightness = float(frame.mean())
                score = chroma + (0.25 * brightness)
                if score > best_score:
                    best, best_score = frame, score
        finally:
            cap.release()
        return best

    def _front_camera_jpeg() -> bytes | None:
        try:
            import cv2
        except ImportError:
            return None
        idx = _resolve_realsense_rgb_index(role="front")
        frame = _read_rgb_frame(cv2, idx)
        if frame is None:
            return None
        chroma = _frame_chroma_bgr(frame)
        camera_diag["front"] = {"index": idx, "chroma": round(chroma, 3), "rgb_like": chroma >= 2.5}
        ok_enc, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok_enc or buf is None:
            return None
        return buf.tobytes()

    def _wrist_camera_jpeg() -> bytes | None:
        nonlocal wrist_last_jpg
        try:
            import cv2
        except ImportError:
            return None
        idx = _resolve_realsense_rgb_index(role="wrist")
        frame = _read_rgb_frame(cv2, idx)
        if frame is None:
            return wrist_last_jpg
        chroma = _frame_chroma_bgr(frame)
        camera_diag["wrist"] = {"index": idx, "chroma": round(chroma, 3), "rgb_like": chroma >= 2.5}
        ok_enc, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok_enc or buf is None:
            return wrist_last_jpg
        wrist_last_jpg = buf.tobytes()
        return wrist_last_jpg

    def _front_mjpeg_stream():
        nonlocal front_last_jpg
        try:
            import cv2
        except ImportError:
            return
        idx = _resolve_realsense_rgb_index(role="front")
        period = max(0.05, float(os.environ.get("D1_FRONT_MJPEG_PERIOD_S", "0.10")))
        cap = None
        while True:
            try:
                if cap is None:
                    cap = _open_rgb_cap(cv2, idx)
                    if not cap.isOpened():
                        cap.release()
                        cap = None
                        if front_last_jpg is not None:
                            yield (
                                b"--frame\r\n"
                                b"Content-Type: image/jpeg\r\n"
                                b"Cache-Control: no-store\r\n\r\n" + front_last_jpg + b"\r\n"
                            )
                        time.sleep(period)
                        continue
                ok, frame = cap.read()
                if not ok or frame is None:
                    if cap is not None:
                        cap.release()
                        cap = None
                    time.sleep(period)
                    continue
                chroma = _frame_chroma_bgr(frame)
                camera_diag["front"] = {"index": idx, "chroma": round(chroma, 3), "rgb_like": chroma >= 2.5}
                ok_enc, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if not ok_enc or buf is None:
                    time.sleep(period)
                    continue
                front_last_jpg = buf.tobytes()
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-store\r\n\r\n" + front_last_jpg + b"\r\n"
                )
                time.sleep(period)
            except GeneratorExit:
                break
            except Exception:
                if cap is not None:
                    cap.release()
                    cap = None
                time.sleep(period)
        if cap is not None:
            cap.release()

    def _wrist_mjpeg_stream():
        nonlocal wrist_last_jpg
        try:
            import cv2
        except ImportError:
            return
        idx = _resolve_realsense_rgb_index(role="wrist")
        period = max(0.05, float(os.environ.get("D1_ORBBEC_LIVE_HTTP_PERIOD_S", "0.10")))
        cap = None
        while True:
            try:
                if cap is None:
                    cap = _open_rgb_cap(cv2, idx)
                    if not cap.isOpened():
                        cap.release()
                        cap = None
                        if wrist_last_jpg is not None:
                            yield (
                                b"--frame\r\n"
                                b"Content-Type: image/jpeg\r\n"
                                b"Cache-Control: no-store\r\n\r\n" + wrist_last_jpg + b"\r\n"
                            )
                        time.sleep(period)
                        continue
                ok, frame = cap.read()
                if not ok or frame is None:
                    if cap is not None:
                        cap.release()
                        cap = None
                    time.sleep(period)
                    continue
                chroma = _frame_chroma_bgr(frame)
                camera_diag["wrist"] = {"index": idx, "chroma": round(chroma, 3), "rgb_like": chroma >= 2.5}
                ok_enc, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if not ok_enc or buf is None:
                    time.sleep(period)
                    continue
                wrist_last_jpg = buf.tobytes()
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-store\r\n\r\n" + wrist_last_jpg + b"\r\n"
                )
                time.sleep(period)
            except GeneratorExit:
                break
            except Exception:
                if cap is not None:
                    cap.release()
                    cap = None
                time.sleep(period)
        if cap is not None:
            cap.release()

    @app.route("/api/front/last.jpg")
    def front_last_jpeg() -> Response:
        jpg = _front_camera_jpeg()
        if jpg is None:
            return Response("front camera unavailable", status=503)
        return Response(jpg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.route("/api/front/live.mjpg")
    def front_live_mjpg() -> Response:
        return Response(
            stream_with_context(_front_mjpeg_stream()),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
        )

    @app.route("/api/cameras/rgb_status")
    def cameras_rgb_status() -> Response:
        wrist_idx = _resolve_realsense_rgb_index(role="wrist")
        front_idx = _resolve_realsense_rgb_index(role="front")
        wrist_live = orbbec_capture.live_rgb_status()
        wrist_status = wrist_live if wrist_live.get("chroma") is not None else camera_diag["wrist"]
        return jsonify(
            {
                "ok": True,
                "wrist": {**wrist_status, "index": wrist_idx, "expected_usb_pid": "0b5c"},
                "front": {**camera_diag["front"], "index": front_idx, "expected_usb_pid": "0b3a"},
                "detection_source": "wrist",
                "depth_nodes_allowed_in_ui": False,
            }
        )

    @app.route("/api/base/sport_last", methods=["GET"])
    def api_base_sport_last() -> Response:
        return jsonify(sport_last_payload())

    @app.route("/api/base/accompany_mode", methods=["GET", "POST"])
    def api_base_accompany_mode() -> Response:
        payload, code = accompany_mode_handle(request)
        return jsonify(payload), code

    @app.route("/api/base/move_nudge", methods=["POST"])
    def api_base_move_nudge() -> Response:
        body = request.get_json(silent=True) or {}
        direction = str(body.get("direction") or "forward").strip().lower()
        duration_s = float(body.get("duration_s", 0.35))
        speed = float(body.get("speed", 0.22))
        yaw_speed = float(body.get("yaw_speed", 0.45))
        vectors: dict[str, tuple[float, float, float]] = {
            "forward": (speed, 0.0, 0.0),
            "backward": (-speed, 0.0, 0.0),
            "left": (0.0, speed, 0.0),
            "right": (0.0, -speed, 0.0),
            "turn_left": (0.0, 0.0, yaw_speed),
            "turn_right": (0.0, 0.0, -yaw_speed),
        }
        if direction not in vectors:
            return jsonify({"ok": False, "reason": "unknown_direction"}), 400
        ok_gate, reason = base_motion_allowed()
        if not ok_gate:
            return jsonify({"ok": False, "reason": reason}), 403
        import sys

        scripts_dir = str(PROJECT_ROOT / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from go2_accompany import sport_move

        vx, vy, vyaw = vectors[direction]
        out = sport_move(
            project_root=PROJECT_ROOT,
            domain=GO2_DDS_DOMAIN,
            iface=(GO2_DDS_INTERFACE.strip() if GO2_DDS_INTERFACE else None),
            vx=vx,
            vy=vy,
            vyaw=vyaw,
            duration_s=max(0.08, min(1.2, duration_s)),
            stand_first=True,
        )
        out["direction"] = direction
        if not out.get("ok"):
            try:
                from go2_dashboard.go2_motor_sport import invoke_dds_sport_ping

                out["sport_probe"] = invoke_dds_sport_ping()
            except Exception as exc:
                out["sport_probe"] = {"ok": False, "reason": repr(exc)}
        return jsonify(out), (200 if out.get("ok") else 502)

    @app.route("/api/motor/thermal_settings", methods=["GET", "POST"])
    def api_motor_thermal_settings() -> Response:
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            if "warn_c" in body:
                THERMAL_SETTINGS["warn_c"] = float(body.get("warn_c"))
            if "critical_c" in body:
                THERMAL_SETTINGS["critical_c"] = float(body.get("critical_c"))
            if "auto_crouch" in body:
                THERMAL_SETTINGS["auto_crouch"] = bool(body.get("auto_crouch"))
        return jsonify({"ok": True, **THERMAL_SETTINGS})

    def _read_motor_temperatures_once(timeout_s: float = 2.2) -> dict[str, Any]:
        import sys

        sys.path.insert(0, str(PROJECT_ROOT / "unitree_sdk2_python"))
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_

        seen: dict[str, Any] = {"temps": None}

        def cb(msg: Any) -> None:
            vals: list[float] = []
            try:
                # Thermal protection applies only to the 12 Go2 leg joints.
                # D1 arm joints must never be folded into the dog thermal gate.
                for i in range(12):
                    st = msg.motor_state[i]
                    t = None
                    if hasattr(st, "temperature"):
                        t = float(getattr(st, "temperature"))
                    elif hasattr(st, "temp"):
                        t = float(getattr(st, "temp"))
                    if t is None:
                        t = 0.0
                    vals.append(t)
            except Exception:
                vals = []
            if vals:
                seen["temps"] = vals

        if GO2_DDS_INTERFACE:
            ChannelFactoryInitialize(GO2_DDS_DOMAIN, GO2_DDS_INTERFACE)
        else:
            ChannelFactoryInitialize(GO2_DDS_DOMAIN)
        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(cb, 8)
        t0 = time.time()
        while time.time() - t0 < timeout_s and seen.get("temps") is None:
            time.sleep(0.08)
        return {"ok": bool(seen.get("temps")), "temps_c": seen.get("temps")}

    @app.route("/api/motor/thermal_status", methods=["GET"])
    def api_motor_thermal_status() -> Response:
        try:
            out = _read_motor_temperatures_once(timeout_s=2.0)
        except Exception as exc:
            return jsonify({"ok": False, "reason": repr(exc), **THERMAL_SETTINGS}), 502
        temps = out.get("temps_c") or []
        max_temp = max(temps) if temps else None
        state = "ok"
        if max_temp is not None and max_temp >= float(THERMAL_SETTINGS["critical_c"]):
            state = "critical"
        elif max_temp is not None and max_temp >= float(THERMAL_SETTINGS["warn_c"]):
            state = "warn"
        return jsonify(
            {
                "ok": out.get("ok", False),
                "state": state,
                "max_temp_c": max_temp,
                "temps_c": temps,
                **THERMAL_SETTINGS,
            }
        )

    @app.route("/api/scan/side_detect", methods=["POST"])
    def scan_side_detect() -> Response:
        body = request.get_json(silent=True) or {}
        side = str(body.get("side") or "right").strip().lower()
        if side not in ("left", "right"):
            return jsonify({"ok": False, "reason": "side_must_be_left_or_right"}), 400
        raw_override = body.get("override_servo_deg")
        if isinstance(raw_override, list) and len(raw_override) >= 7:
            target = service.clamp_servo_deg([float(x) for x in raw_override[:7]])
        else:
            target = _scan_side_target(side)
        program_runner.request_stop()
        service.motion_reset()
        transit = _safe_transit_target()
        if transit is None:
            return jsonify(
                {
                    "ok": False,
                    "reason": "safe_transit_unavailable",
                    "safety_interlock": True,
                    "scan_side": side,
                    "target_servo_deg": target,
                }
            ), 503
        service._halt_cartesian_stream(wait_idle=True)
        program_runner.clear_stop_request()
        fb_before = service.read_servo_deg(fast=True)
        couple = service.ensure_coupled_for_motion()
        if not couple.get("ok"):
            return jsonify(
                {
                    "ok": False,
                    "reason": "couple_failed",
                    "coupling": couple,
                    "feedback_before": fb_before,
                    "target_servo_deg": target,
                    "scan_side": side,
                }
            ), 502
        move = None
        transit_move = None
        transit_move = _move_to_point_with_busy_recovery(transit)
        if not (transit_move.get("ok") or transit_move.get("skipped")):
            transit_move["action"] = transit_move.get("action") or "scan_side_transit"
            transit_move["target_servo_deg"] = transit
            transit_move["scan_side"] = side
            transit_move["coupling"] = couple
            transit_move["phase"] = "fold_before_rotate"
            return jsonify(
                {
                    "ok": False,
                    "reason": transit_move.get("reason", "transit_failed"),
                    "scan_side": side,
                    "target_servo_deg": target,
                    "coupling": couple,
                    "feedback_before": fb_before,
                    "transit_zero": transit,
                    "transit_move": transit_move,
                }
            ), 502
        side_transit = transit[:]
        side_transit[0] = float(target[0])
        side_transit_move = _move_to_point_with_busy_recovery(side_transit)
        if not (side_transit_move.get("ok") or side_transit_move.get("skipped")):
            side_transit_move["phase"] = "rotate_while_folded"
            return jsonify(
                {
                    "ok": False,
                    "reason": side_transit_move.get("reason", "side_transit_failed"),
                    "scan_side": side,
                    "target_servo_deg": target,
                    "coupling": couple,
                    "feedback_before": fb_before,
                    "transit_zero": transit,
                    "transit_move": transit_move,
                    "side_transit": side_transit,
                    "side_transit_move": side_transit_move,
                }
            ), 502
        move = _move_to_point_with_busy_recovery(target)
        move["action"] = move.get("action") or "scan_side_move"
        move["target_servo_deg"] = target
        move["scan_side"] = side
        move["transit_zero"] = transit
        move["transit_move"] = transit_move
        move["side_transit"] = side_transit
        move["side_transit_move"] = side_transit_move
        settle_s = float(os.environ.get("D1_SCAN_SIDE_SETTLE_S", "1.1"))
        if settle_s > 0:
            time.sleep(settle_s)
        fb_mid = service.read_servo_deg(fast=True)
        max_error = None
        if isinstance(fb_mid.get("servo_deg"), list) and len(fb_mid["servo_deg"]) >= 7:
            errs = [abs(float(fb_mid["servo_deg"][i]) - float(target[i])) for i in range(7)]
            max_error = max(errs) if errs else None
        move["feedback_mid"] = fb_mid
        move["max_error_deg"] = max_error
        if not (move.get("ok") or move.get("skipped")):
            move["coupling"] = couple
            move["feedback_before"] = fb_before
            return jsonify(move), 502
        if os.environ.get("D1_GRASP6D_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}:
            detect = _capture_grasp6d_plan()
            grasp_estimate = detect.get("grasp_estimate") or {}
        else:
            detect = pick_vision.capture_and_detect()
            _apply_pick_detection_to_preset(detect)
            grasp_estimate = _scan_grasp_estimate(target, detect)
        ok_move = bool(move.get("ok") or move.get("skipped"))
        ok_all = ok_move and bool(detect.get("ok"))
        fb_after = service.read_servo_deg(fast=True)
        return jsonify(
            {
                "ok": ok_all,
                "scan_side": side,
                "target_servo_deg": target,
                "transit_zero": transit,
                "coupling": couple,
                "feedback_before": fb_before,
                "feedback_mid": fb_mid,
                "feedback_after": fb_after,
                "move": move,
                "detection": detect,
                "grasp_estimate": grasp_estimate,
            }
        ), (200 if ok_all else 502)

    @app.route("/assets/<string:name>")
    def dashboard_asset(name: str) -> Response:
        allowed = {
            "unitree_robot_main.png": PROJECT_ROOT / "data" / "unitree_robot_main.png",
            "unitree_go2_hero.png": PROJECT_ROOT / "data" / "unitree_go2_hero.png",
            "unitree_go2_features.png": PROJECT_ROOT / "data" / "unitree_go2_features.png",
        }
        path = allowed.get(name)
        if path is None or not path.is_file():
            return Response("asset not found", status=404)
        return send_file(path, conditional=True, max_age=3600)

    @app.route("/api/joints/feedback")
    def joints_feedback() -> Response:
        return jsonify(service.read_servo_deg())

    @app.route("/api/joints/jog", methods=["POST"])
    def joints_jog() -> Response:
        body = request.get_json(silent=True) or {}
        raw = body.get("servo_deg")
        if not isinstance(raw, list) or len(raw) < 6:
            return jsonify({"ok": False, "reason": "servo_deg required (6-7 floats)"}), 400
        try:
            servo = [float(x) for x in raw[:7]]
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "servo_deg must be numeric"}), 400
        while len(servo) < 7:
            servo.append(servo[-1])
        if body.get("joint_index") is not None:
            try:
                ji = int(body["joint_index"])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "reason": "joint_index_invalid"}), 400
            if ji < 0 or ji > 6:
                return jsonify({"ok": False, "reason": "joint_index_out_of_range"}), 400
            servo = service.merge_single_joint_jog(servo, ji)
        with_enable = bool(body.get("with_enable"))
        if with_enable:
            out = service.jog_with_enable(servo)
        else:
            out = service.jog_pose_deg(servo, keep_lock=bool(body.get("session")))
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/joints/session_begin", methods=["POST"])
    def joints_session_begin() -> Response:
        body = request.get_json(silent=True) or {}
        servo, err = _servo_deg_from_body(body) if body.get("servo_deg") else (None, None)
        if err:
            return jsonify({"ok": False, "reason": err}), 400
        return jsonify(service.joint_control_begin(servo_deg=servo))

    @app.route("/api/joints/session_end", methods=["POST"])
    def joints_session_end() -> Response:
        return jsonify(service.joint_control_end())

    @app.route("/api/joints/hold", methods=["POST"])
    def joints_hold() -> Response:
        body = request.get_json(silent=True) or {}
        raw = body.get("servo_deg")
        servo: list[float] | None = None
        if isinstance(raw, list) and len(raw) >= 6:
            try:
                servo = service.clamp_servo_deg([float(x) for x in raw[:7]])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "reason": "servo_deg_invalid"}), 400
        out = service.hold_pose_stream(servo_deg=servo)
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/joints/hold_now", methods=["POST"])
    def joints_hold_now() -> Response:
        body = request.get_json(silent=True) or {}
        reason = str(body.get("reason") or "ui").strip() or "ui"
        out = service.request_emergency_hold(reason=reason)
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/joints/enable", methods=["POST"])
    def joints_enable() -> Response:
        body = request.get_json(silent=True) or {}
        mode = int(body.get("mode", 1))
        with_power = bool(body.get("with_power", False))
        out = service.enable_all(mode=mode, with_power=with_power)
        out["funcode"] = "6+5" if with_power else 5
        out["action"] = "enable_motors"
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/joints/power", methods=["POST"])
    def joints_power() -> Response:
        out = service.arm_power_on()
        out["funcode"] = 6
        out["action"] = "arm_power_on"
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/joints/release", methods=["POST"])
    def joints_release() -> Response:
        body = request.get_json(silent=True) or {}
        if body.get("confirm") != "RELEASE_ARM_TORQUE":
            return jsonify(
                {
                    "ok": False,
                    "reason": "explicit_release_confirmation_required",
                    "required_confirm": "RELEASE_ARM_TORQUE",
                    "safety": "Release disabilitato per richieste accidentali: toglie coppia e fa cadere il braccio.",
                }
            ), 409
        out = service.motor_release()
        out["funcode"] = 5
        out["action"] = "motor_release"
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/joints/zero", methods=["POST"])
    def joints_zero() -> Response:
        program_runner.clear_stop_request()
        out = service.go_zero()
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/arm/true_zero", methods=["GET", "POST"])
    def arm_true_zero() -> Response:
        if request.method == "GET":
            return jsonify(service.true_zero_pose_info())
        body = request.get_json(silent=True) or {}
        op = str(body.get("op") or body.get("action") or "goto").strip().lower()
        if op in {"save", "memorize", "store"}:
            servo, err = _servo_deg_from_body(body)
            if servo is None:
                return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
            out = service.save_true_zero_pose(servo_deg=servo)
            code = 200 if out.get("ok") else 502
            return jsonify(out), code
        if op in {"goto", "goto_zero", "move", "transit"}:
            program_runner.clear_stop_request()
            out = service.goto_true_zero_pose()
            code = 200 if out.get("ok") or out.get("skipped") else 502
            return jsonify(out), code
        return jsonify({"ok": False, "reason": "unsupported_true_zero_op", "op": op}), 400

    @app.route("/api/arm/status")
    def arm_status() -> Response:
        return jsonify({"ok": True, "arm_coupled": service.arm_coupled()})

    @app.route("/api/arm/couple", methods=["POST"])
    def arm_couple() -> Response:
        body = request.get_json(silent=True) or {}
        feedback = service.read_servo_deg(fast=True)
        if feedback.get("ok") and feedback.get("servo_deg"):
            out = service.couple_and_hold_pose(
                feedback["servo_deg"],
                with_power=bool(body.get("with_power")),
                force=bool(body.get("force", True)),
            )
            out["feedback_before"] = feedback
        else:
            out = service.ensure_coupled(
                with_power=bool(body.get("with_power")),
                force=bool(body.get("force")),
            )
            out["feedback_before"] = feedback
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/arm/maintain", methods=["POST"])
    def arm_maintain() -> Response:
        """Rinnova coppia dopo cambio pagina / movimento — mai release."""
        body = request.get_json(silent=True) or {}
        raw = body.get("servo_deg")
        servo: list[float] | None = None
        if isinstance(raw, list) and len(raw) >= 6:
            try:
                servo = service.clamp_servo_deg([float(x) for x in raw[:7]])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "reason": "servo_deg_invalid"}), 400
        out = service.hold_pose_stream(servo_deg=servo)
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    def _servo_deg_from_body(body: dict) -> tuple[list[float] | None, str | None]:
        raw = body.get("servo_deg")
        if isinstance(raw, list) and len(raw) >= 6:
            try:
                sd = [float(x) for x in raw[:7]]
                while len(sd) < 7:
                    sd.append(sd[-1] if sd else 0.0)
                return service.clamp_servo_deg(sd), None
            except (TypeError, ValueError):
                return None, "servo_deg_invalid"
        fb = service.read_servo_deg(fast=True)
        if not fb.get("ok") or not fb.get("servo_deg"):
            return None, str(fb.get("reason", "no_feedback"))
        return fb["servo_deg"], None

    @app.route("/api/cartesian/pose")
    def cartesian_pose() -> Response:
        cached = service.get_servo_cache()
        if cached is not None:
            pose = cartesian.tcp_pose_m(cached)
            return jsonify({"ok": True, "servo_deg": cached, "cached": True, **pose})
        fb = service.read_servo_deg(fast=True)
        if not fb.get("ok") or not fb.get("servo_deg"):
            return jsonify({"ok": False, "reason": fb.get("reason", "no_feedback")}), 503
        pose = cartesian.tcp_pose_m(fb["servo_deg"])
        return jsonify({"ok": True, "servo_deg": fb["servo_deg"], **pose})

    @app.route("/api/cartesian/jog_start", methods=["POST"])
    def cartesian_jog_start() -> Response:
        body = request.get_json(silent=True) or {}
        raw_sd = body.get("servo_deg")
        if isinstance(raw_sd, list) and len(raw_sd) >= 6:
            try:
                servo_deg = service.clamp_servo_deg([float(x) for x in raw_sd[:7]])
                err = None
            except (TypeError, ValueError):
                servo_deg, err = None, "servo_deg_invalid"
        else:
            servo_deg, err = None, None
        if servo_deg is None:
            cached = service.get_servo_cache()
            if cached is not None:
                servo_deg = cached
            else:
                fb = service.read_servo_deg(fast=True)
                if not fb.get("ok") or not fb.get("servo_deg"):
                    return jsonify({"ok": False, "reason": fb.get("reason", "no_feedback")}), 503
                servo_deg = fb["servo_deg"]
        try:
            vel = float(body.get("velocity_pct", body.get("speed_pct", 30)))
            max_sp = body.get("max_speed_mm_s")
            max_speed = float(max_sp) if max_sp is not None else None
            acc = body.get("accel_mm_s2")
            dec = body.get("decel_mm_s2")
            accel = float(acc) if acc is not None else None
            decel = float(dec) if dec is not None else None
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "invalid_numeric_params"}), 400
        out = service.cartesian_begin_jog(
            axis=str(body.get("axis", "x")),
            sign=float(body.get("sign", 1)),
            velocity_pct=vel,
            max_speed_mm_s=max_speed,
            accel_mm_s2=accel,
            decel_mm_s2=decel,
            servo_deg=servo_deg,
        )
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    @app.route("/api/cartesian/move_tcp", methods=["POST"])
    def cartesian_move_tcp() -> Response:
        body = request.get_json(silent=True) or {}
        try:
            delta_mm = float(body.get("delta_mm", body.get("step_mm", 10)))
            sign = float(body.get("sign", 1))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "delta_mm and sign must be numeric"}), 400
        out = service.cartesian_move_tcp(
            axis=str(body.get("axis", "x")),
            sign=sign,
            delta_mm=delta_mm,
        )
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/cartesian/jog_stop", methods=["POST"])
    def cartesian_jog_stop() -> Response:
        body = request.get_json(silent=True) or {}
        out = service.cartesian_end_jog(hold_after=bool(body.get("hold_after")))
        return jsonify(out)

    @app.route("/api/cartesian/jog_update", methods=["POST"])
    def cartesian_jog_update() -> Response:
        body = request.get_json(silent=True) or {}
        try:
            vel = body.get("velocity_pct", body.get("speed_pct"))
            velocity_pct = float(vel) if vel is not None else None
            max_sp = body.get("max_speed_mm_s")
            max_speed = float(max_sp) if max_sp is not None else None
            acc = body.get("accel_mm_s2")
            dec = body.get("decel_mm_s2")
            accel = float(acc) if acc is not None else None
            decel = float(dec) if dec is not None else None
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "invalid_numeric_params"}), 400
        out = jog_stream.jog_update(
            velocity_pct=velocity_pct,
            max_speed_mm_s=max_speed,
            accel_mm_s2=accel,
            decel_mm_s2=decel,
        )
        code = 200 if out.get("ok") else 409
        return jsonify(out), code

    @app.route("/api/cartesian/jog_status")
    def cartesian_jog_status() -> Response:
        return jsonify(jog_stream.jog_status())

    @app.route("/api/cartesian/jog_tick", methods=["POST"])
    def cartesian_jog_tick_route() -> Response:
        body = request.get_json(silent=True) or {}
        servo_deg, err = _servo_deg_from_body(body)
        if servo_deg is None:
            return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
        try:
            vel = float(body.get("velocity_pct", body.get("speed_pct", 25)))
            dt_s = float(body.get("dt_s", 0.04))
            max_sp = body.get("max_speed_mm_s")
            max_speed = float(max_sp) if max_sp is not None else None
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "velocity_pct and dt_s must be numeric"}), 400
        out = cartesian.cartesian_jog_tick(
            servo_deg,
            axis=str(body.get("axis", "x")),
            sign=float(body.get("sign", 1)),
            velocity_pct=vel,
            dt_s=dt_s,
            max_speed_mm_s=max_speed,
        )
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/cartesian/nudge", methods=["POST"])
    def cartesian_nudge() -> Response:
        jog_stream.jog_stop()
        body = request.get_json(silent=True) or {}
        servo_deg, err = _servo_deg_from_body(body)
        if servo_deg is None:
            return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
        axis = str(body.get("axis", "x"))
        sign = body.get("sign", 1)
        step_mm = body.get("step_mm")
        interp = body.get("interpolated")
        try:
            smm = float(step_mm) if step_mm is not None else None
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "step_mm must be numeric"}), 400
        out = cartesian.cartesian_nudge(
            servo_deg,
            axis=axis,
            sign=float(sign),
            step_mm=smm,
            interpolated=interp if interp is None else bool(interp),
        )
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/programs", methods=["GET"])
    def programs_list() -> Response:
        return jsonify({"ok": True, "programs": program_store.list_programs()})

    @app.route("/api/programs", methods=["POST"])
    def programs_create() -> Response:
        body = request.get_json(silent=True) or {}
        name = str(body.get("name", "Programma"))
        prog = program_store.create_program(name)
        return jsonify({"ok": True, "program": prog})

    @app.route("/api/programs/<program_id>", methods=["GET"])
    def programs_get(program_id: str) -> Response:
        prog = program_store.load_program(program_id)
        if prog is None:
            return jsonify({"ok": False, "reason": "program_not_found"}), 404
        return jsonify({"ok": True, "program": prog})

    @app.route("/api/programs/<program_id>", methods=["DELETE"])
    def programs_delete(program_id: str) -> Response:
        if program_store.delete_program(program_id):
            return jsonify({"ok": True})
        return jsonify({"ok": False, "reason": "program_not_found"}), 404

    @app.route("/api/programs/<program_id>/waypoints", methods=["POST"])
    def programs_add_waypoint(program_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        servo_deg, err = _servo_deg_from_body(body)
        if servo_deg is None:
            return jsonify({"ok": False, "reason": err or "servo_deg_required"}), 400
        tcp_pose = cartesian.tcp_pose_m(servo_deg)
        prog, wp = program_store.add_waypoint(
            program_id,
            name=str(body.get("name", "")) or None,
            servo_deg=servo_deg,
            tcp_pose=tcp_pose,
        )
        if prog is None:
            return jsonify({"ok": False, "reason": "program_not_found"}), 404
        return jsonify({"ok": True, "program": prog, "waypoint": wp})

    @app.route("/api/programs/<program_id>/teach_capture", methods=["POST"])
    def programs_teach_capture(program_id: str) -> Response:
        """Cattura una posa in release, riattiva subito HOLD, poi la persiste.

        L'ordine e' intenzionale: il braccio viene messo in sicurezza prima di
        qualsiasi scrittura su disco. Il publisher resta il daemon hold esterno
        usato da ``service.couple_and_hold_pose``.
        """
        if program_store.load_program(program_id) is None:
            return jsonify({"ok": False, "reason": "program_not_found"}), 404
        body = request.get_json(silent=True) or {}
        feedback = service.read_servo_deg(fast=False)
        raw = feedback.get("servo_deg")
        if not feedback.get("ok") or not isinstance(raw, list) or len(raw) < 7:
            return jsonify(
                {
                    "ok": False,
                    "reason": feedback.get("reason", "no_feedback_in_release"),
                    "feedback": feedback,
                    "safety": "Sostieni il braccio e premi HOLD ORA: la posa non e' stata salvata.",
                }
            ), 503
        taught = service.clamp_servo_deg([float(x) for x in raw[:7]])
        hold = service.couple_and_hold_pose(taught, with_power=True, force=True)
        if not (hold.get("ok") or hold.get("skipped")):
            return jsonify(
                {
                    "ok": False,
                    "reason": hold.get("reason", "hold_failed"),
                    "feedback": feedback,
                    "hold": hold,
                    "safety": "Posa non salvata perche' HOLD non e' stato confermato.",
                }
            ), 502
        tcp_pose = cartesian.tcp_pose_m(taught)
        prog, wp = program_store.add_waypoint(
            program_id,
            name=str(body.get("name", "")) or None,
            servo_deg=taught,
            tcp_pose=tcp_pose,
        )
        if prog is None:
            return jsonify(
                {
                    "ok": False,
                    "reason": "program_save_failed_after_hold",
                    "hold": hold,
                    "held_servo_deg": taught,
                }
            ), 500
        return jsonify(
            {
                "ok": True,
                "program": prog,
                "waypoint": wp,
                "held_servo_deg": taught,
                "feedback": feedback,
                "hold": hold,
            }
        )

    @app.route("/api/programs/<program_id>/waypoints/<waypoint_id>", methods=["DELETE"])
    def programs_delete_waypoint(program_id: str, waypoint_id: str) -> Response:
        prog = program_store.delete_waypoint(program_id, waypoint_id)
        if prog is None:
            return jsonify({"ok": False, "reason": "program_not_found"}), 404
        return jsonify({"ok": True, "program": prog})

    @app.route("/api/orbbec/capture", methods=["POST"])
    def orbbec_capture_frame() -> Response:
        out = orbbec_capture.capture_orbbec_jpeg()
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    @app.route("/api/orbbec/live.mjpg")
    def orbbec_live_mjpeg() -> Response:
        period = max(0.20, float(os.environ.get("D1_ORBBEC_LIVE_HTTP_PERIOD_S", "0.35")))

        def generate_fallback():
            last: bytes | None = None
            while True:
                try:
                    cap = orbbec_capture.capture_orbbec_jpeg()
                    path = orbbec_capture.latest_snapshot_path()
                    if cap.get("ok") and path is not None and path.is_file():
                        jpg = path.read_bytes()
                        if jpg:
                            last = jpg
                except Exception:
                    pass
                if last:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Cache-Control: no-store\r\n\r\n" + last + b"\r\n"
                    )
                time.sleep(period)

        def generate_combined():
            yielded = False
            for chunk in orbbec_capture.generate_rgb_mjpeg_stream():
                yielded = True
                yield chunk
            if yielded:
                return
            yield from generate_fallback()

        return Response(
            stream_with_context(generate_combined()),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
        )

    @app.route("/api/orbbec/last.jpg")
    def orbbec_last_jpeg() -> Response:
        jpg = _wrist_camera_jpeg()
        if jpg is not None:
            return Response(jpg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
        path = orbbec_capture.latest_snapshot_path()
        if path is None:
            return jsonify({"ok": False, "reason": "no_snapshot"}), 404
        return send_file(path, mimetype="image/jpeg", max_age=0)

    @app.route("/api/orbbec/probe")
    def orbbec_probe() -> Response:
        from go2_dashboard.cameras import _v4l_sysfs_card_name

        order = orbbec_capture._v4l_indices_probe_order()
        nodes = [
            {
                "index": idx,
                "sysfs_name": _v4l_sysfs_card_name(idx),
                "ir_sysfs": orbbec_capture._v4l_sysfs_name_is_ir(_v4l_sysfs_card_name(idx)),
            }
            for idx in order
        ]
        chosen = orbbec_capture.resolve_orbbec_rgb_v4l_index(force_probe=True)
        chroma_map = {}
        spread_map = {}
        for idx in orbbec_capture.orbbec_all_v4l_indices():
            spread, chroma = orbbec_capture._probe_index_rgb_quality(idx)
            chroma_map[idx] = round(chroma, 2)
            spread_map[idx] = round(spread, 2)
        return jsonify(
            {
                "ok": chosen is not None,
                "probe_order": order,
                "orbbec_nodes": orbbec_capture.orbbec_all_v4l_indices(),
                "chroma_by_index": chroma_map,
                "spread_by_index": spread_map,
                "min_chroma_rgb": orbbec_capture._orbbec_min_frame_chroma(),
                "min_channel_spread": orbbec_capture._orbbec_min_channel_spread(),
                "nodes": nodes,
                "chosen_v4l_index": chosen,
                "pinned_v4l_index": orbbec_capture._pinned_rgb_v4l_index(),
                "auto_discovery": orbbec_capture._auto_discovery_enabled(),
                "rgb_only": orbbec_capture._rgb_only(),
                "stream_kind": "rgb" if chosen is not None else "none",
            }
        )

    @app.route("/api/pick/preset", methods=["GET"])
    def pick_preset_get() -> Response:
        return jsonify(pick_preset.preset_info())

    @app.route("/api/pick/tuning", methods=["GET", "POST"])
    def pick_tuning() -> Response:
        if request.method == "GET":
            return jsonify(pick_preset.tuning_info())
        body = request.get_json(silent=True) or {}
        try:
            out = pick_preset.set_tuning(body)
        except ValueError as exc:
            return jsonify({"ok": False, "reason": str(exc)}), 400
        return jsonify(out)

    @app.route("/api/pick/preset", methods=["POST"])
    def pick_preset_set() -> Response:
        body = request.get_json(silent=True) or {}
        if body.get("from_program"):
            derived = pick_preset.offsets_from_program_waypoints()
            if not derived.get("ok"):
                return jsonify(derived), 404
            info = pick_preset.set_offsets(
                derived["joint_offset_deg"],
                source="program_delta",
            )
            info["derived"] = derived
            return jsonify(info)
        if "manual_orient_offset_deg" in body and body.get("joint_offset_deg") is None:
            try:
                info = pick_preset.set_manual_orient_offset_deg(
                    float(body.get("manual_orient_offset_deg", 0)),
                )
            except (TypeError, ValueError):
                return jsonify({"ok": False, "reason": "manual_orient_offset_deg_invalid"}), 400
            return jsonify(info)
        raw = body.get("joint_offset_deg")
        if not isinstance(raw, list) or len(raw) < 6:
            return jsonify({"ok": False, "reason": "joint_offset_deg_required"}), 400
        try:
            off = [float(x) for x in raw[:7]]
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "joint_offset_deg_invalid"}), 400
        last_det = body.get("last_detection")
        info = pick_preset.set_offsets(
            off,
            source=str(body.get("source", "manual")),
            last_detection=last_det if isinstance(last_det, dict) else None,
        )
        if "manual_orient_offset_deg" in body:
            try:
                info = pick_preset.set_manual_orient_offset_deg(
                    float(body.get("manual_orient_offset_deg", 0)),
                )
            except (TypeError, ValueError):
                return jsonify({"ok": False, "reason": "manual_orient_offset_deg_invalid"}), 400
        return jsonify(info)

    @app.route("/api/pick/preset/from_pose", methods=["POST"])
    def pick_preset_from_pose() -> Response:
        """Salva offset = posa attuale − SCANSIONE (dopo jog in teach)."""
        body = request.get_json(silent=True) or {}
        servo, err = _servo_deg_from_body(body)
        if servo is None:
            return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
        out = pick_preset.offsets_from_current_vs_scan(servo)
        code = 200 if out.get("ok") else 404
        return jsonify(out), code

    @app.route("/api/pick/teach/samples", methods=["GET"])
    def pick_teach_samples_list() -> Response:
        from go2_dashboard.d1_jog import pick_teach_model

        return jsonify(pick_teach_model.list_teach_samples())

    @app.route("/api/pick/teach/finish", methods=["POST"])
    def pick_teach_finish() -> Response:
        """Salva un esempio teach (dopo release) e attiva coppia sulla posa insegnata."""
        from go2_dashboard.d1_jog import pick_teach_model

        body = request.get_json(silent=True) or {}
        vis = body.get("vision_at_scan")
        if body.get("servo_deg"):
            servo, err = _servo_deg_from_body(body)
            if servo is None:
                return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
            out = pick_teach_model.finish_teach_sample_after_release(
                vision_at_scan=vis if isinstance(vis, dict) else None,
                taught_servo_deg=servo,
            )
        else:
            out = pick_teach_model.finish_teach_sample_after_release(
                vision_at_scan=vis if isinstance(vis, dict) else None,
            )
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    @app.route("/api/pick/teach/samples/<sample_id>", methods=["DELETE"])
    def pick_teach_sample_delete(sample_id: str) -> Response:
        from go2_dashboard.d1_jog import pick_teach_model

        out = pick_teach_model.delete_teach_sample(sample_id)
        code = 200 if out.get("ok") else 404
        return jsonify(out), code

    @app.route("/api/pick/teach/build_model", methods=["POST"])
    def pick_teach_build_model() -> Response:
        from go2_dashboard.d1_jog import pick_teach_model

        out = pick_teach_model.build_teach_model()
        code = 200 if out.get("ok") else 400
        return jsonify(out), code

    @app.route("/api/pick/calibrate/zero/finish", methods=["POST"])
    def pick_calibrate_zero_finish() -> Response:
        """Chiude calibrazione: coppia ON (fine task) + offset + riferimento visione."""
        body = request.get_json(silent=True) or {}
        vis = body.get("vision_at_scan")
        if body.get("servo_deg"):
            servo, err = _servo_deg_from_body(body)
            if servo is None:
                return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
            out = pick_preset.finish_zero_calibration_after_release(
                vision_at_scan=vis if isinstance(vis, dict) else None,
                taught_servo_deg=servo,
            )
        else:
            out = pick_preset.finish_zero_calibration_after_release(
                vision_at_scan=vis if isinstance(vis, dict) else None,
            )
        code = 200 if (out.get("ok") or out.get("has_zero_calibration")) else 502
        return jsonify(out), code

    @app.route("/api/pick/preset/nudge", methods=["POST"])
    def pick_preset_nudge() -> Response:
        body = request.get_json(silent=True) or {}
        try:
            joint = int(body.get("joint", body.get("joint_index", 0)))
            delta = float(body.get("delta_deg", 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "joint_and_delta_deg_required"}), 400
        if delta == 0:
            return jsonify({"ok": False, "reason": "delta_deg_zero"}), 400
        out = pick_preset.nudge_offsets(joint_index=joint, delta_deg=delta)
        code = 200 if out.get("ok") else 400
        return jsonify(out), code

    def _apply_pick_detection_to_preset(out: dict[str, Any]) -> None:
        if not out.get("ok") or not out.get("last_detection"):
            return
        preset = pick_preset.load_preset()
        if preset.get("joint_offset_deg"):
            pick_preset.set_offsets(
                preset["joint_offset_deg"],
                source=preset.get("source", "unchanged"),
                last_detection=out["last_detection"],
            )
        else:
            derived = pick_preset.offsets_from_program_waypoints()
            if derived.get("ok"):
                pick_preset.set_offsets(
                    derived["joint_offset_deg"],
                    source="program_delta",
                    last_detection=out["last_detection"],
                )

    def _scan_grasp_estimate(scan_servo_deg: list[float], vision: dict[str, Any]) -> dict[str, Any]:
        """Dati leggibili per UI: pixel, posa di presa e distanza solo se calibrata."""
        detection = vision.get("detection") if isinstance(vision.get("detection"), dict) else {}
        last_detection = vision.get("last_detection") if isinstance(vision.get("last_detection"), dict) else {}
        preset = pick_preset.load_preset()
        offsets = pick_preset.effective_joint_offsets(last_detection=last_detection)
        grasp_servo = (
            pick_preset.grasp_servo_from_scan(
                scan_servo_deg,
                offsets=offsets,
                last_detection=last_detection,
            )
            if offsets is not None
            else None
        )
        center = detection.get("grip_center_px") or detection.get("bbox_center_px")
        bbox = detection.get("bbox_xyxy")
        known_width_m = max(0.0, float(os.environ.get("D1_BLUE_BOX_WIDTH_M", "0")))
        focal_px = max(1.0, float(os.environ.get("D1_WRIST_RGB_FX_PX", "615")))
        distance_m = None
        if known_width_m > 0 and isinstance(bbox, list) and len(bbox) >= 4:
            width_px = abs(float(bbox[2]) - float(bbox[0]))
            if width_px >= 2:
                distance_m = round((focal_px * known_width_m) / width_px, 3)
        tcp = None
        if isinstance(grasp_servo, list):
            try:
                from go2_dashboard.d1_jog import cartesian

                tcp = cartesian.tcp_pose_m(grasp_servo)
            except Exception:
                tcp = None
        return {
            "detected": bool(vision.get("detection_ok")),
            "label": detection.get("label"),
            "confidence": detection.get("confidence"),
            "center_px": center,
            "normalized_xy": detection.get("norm"),
            "orientation_deg": detection.get("orientation_deg"),
            "bbox_xyxy": bbox,
            "metric_distance_m": distance_m,
            "metric_distance_available": distance_m is not None,
            "distance_note_it": (
                "Stima pinhole da larghezza scatola calibrata."
                if distance_m is not None
                else "Distanza metrica non disponibile dal solo RGB: configura D1_BLUE_BOX_WIDTH_M o usa depth allineata."
            ),
            "known_box_width_m": known_width_m or None,
            "effective_joint_offsets_deg": offsets,
            "grasp_servo_deg": grasp_servo,
            "grasp_tcp_estimate": tcp,
        }

    @app.route("/api/pick/vision/crop", methods=["GET"])
    def pick_vision_crop_get() -> Response:
        from go2_dashboard.d1_jog import pick_vision_crop

        return jsonify(pick_vision_crop.crop_settings_info())

    @app.route("/api/pick/vision/crop", methods=["POST"])
    def pick_vision_crop_set() -> Response:
        from go2_dashboard.d1_jog import pick_vision_crop

        body = request.get_json(silent=True) or {}
        fr = body.get("crop_fracs") if isinstance(body.get("crop_fracs"), dict) else body
        if not isinstance(fr, dict):
            return jsonify({"ok": False, "reason": "crop_fracs_required"}), 400
        saved = pick_vision_crop.save_crop_fracs(fr)
        return jsonify({"ok": True, **pick_vision_crop.crop_settings_info(), "saved": saved})

    @app.route("/api/pick/vision/crop/preview", methods=["POST"])
    def pick_vision_crop_preview() -> Response:
        """Solo ROI sulla foto salvata (senza YOLO) — per regolare i bordi."""
        from go2_dashboard.d1_jog import pick_vision_crop

        body = request.get_json(silent=True) or {}
        if isinstance(body.get("crop_fracs"), dict):
            pick_vision_crop.save_crop_fracs(body["crop_fracs"])
        snap = orbbec_capture.latest_snapshot_path()
        if snap is None or not snap.is_file():
            return jsonify({"ok": False, "reason": "no_snapshot", "hint": "Fai prima una foto"}), 404
        frame = pick_vision._read_bgr_from_jpeg(snap)
        if frame is None:
            return jsonify({"ok": False, "reason": "decode_failed"}), 502
        _, _, roi = pick_vision_crop.crop_frame_for_detection(frame)
        overlay = pick_vision_crop.draw_crop_roi_outline(frame, roi)
        try:
            import cv2
        except ImportError:
            return jsonify({"ok": False, "reason": "cv2_unavailable"}), 502
        quality = int(os.environ.get("D1_ORBBEC_JPEG_QUALITY", "88"))
        ok_enc, buf = cv2.imencode(".jpg", overlay, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok_enc or buf is None:
            return jsonify({"ok": False, "reason": "encode_failed"}), 502
        pick_vision.scene_overlay_path().parent.mkdir(parents=True, exist_ok=True)
        pick_vision.scene_overlay_path().write_bytes(buf.tobytes())
        ts = int(time.time())
        return jsonify(
            {
                "ok": True,
                "preview_url": f"/api/pick/scene.jpg?t={ts}",
                "roi_px": list(roi),
                "crop_fracs": pick_vision_crop.vision_crop_fracs(),
            }
        )

    def _public_box6d(box: dict[str, Any]) -> dict[str, Any]:
        out = dict(box)
        for key in ("T_camera_box",):
            value = out.get(key)
            if hasattr(value, "tolist"):
                out[key] = value.tolist()
        plane = out.get("plane")
        if isinstance(plane, dict):
            plane = dict(plane)
            if hasattr(plane.get("normal"), "tolist"):
                plane["normal"] = plane["normal"].tolist()
            out["plane"] = plane
        return out

    def _capture_grasp6d_plan() -> dict[str, Any]:
        try:
            frame = wrist_rgbd.capture_aligned()
        except Exception as exc:
            return {
                "ok": False,
                "reason": "wrist_rgbd_capture_failed",
                "detail": str(exc),
                "rgbd": wrist_rgbd.health(),
            }
        box = grasp6d.estimate_box_pose(frame.depth_m, frame.intrinsics)
        public_box = _public_box6d(box)
        if not box.get("ok"):
            return {"ok": False, "reason": box.get("reason"), "rgbd": frame.public_info(), "box": public_box}
        feedback = service.read_servo_deg(fast=True)
        if not feedback.get("ok") or not isinstance(feedback.get("servo_deg"), list):
            return {"ok": False, "reason": "arm_feedback_unavailable", "feedback": feedback, "box": public_box}
        plan = grasp6d.plan_grasp(box, current_servo_deg=feedback["servo_deg"])
        return {
            "ok": bool(plan.get("ok")),
            "reason": plan.get("reason"),
            "source": "rgbd_cuboid_6d",
            "rgbd": frame.public_info(),
            "box": public_box,
            "plan": plan,
            "feedback": feedback,
            "grasp_estimate": {
                "detected": bool(box.get("ok")),
                "label": "scatola 3D",
                "metric_distance_m": round(float(box["center_camera_m"][2]), 3),
                "metric_distance_available": True,
                "grasp_servo_deg": ((plan.get("selected") or {}).get("grasp") or {}).get("servo_deg"),
                "grasp_tcp_estimate": {
                    "xyz_m": [
                        row[3]
                        for row in ((plan.get("selected") or {}).get("T_base_grasp") or [[0, 0, 0, 0]] * 4)[:3]
                    ],
                    "frame": "arm_base",
                },
            },
        }

    def _save_grasp6d_run(payload: dict[str, Any]) -> None:
        import json

        path = PROJECT_ROOT / "data" / "d1_grasp6d_last.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    @app.route("/api/pick/rgbd/health", methods=["GET", "POST"])
    def pick_rgbd_health() -> Response:
        capture = request.method == "POST" or request.args.get("capture") in {"1", "true", "yes"}
        out = wrist_rgbd.health(capture=capture)
        return jsonify(out), (200 if out.get("ok") else 503)

    @app.route("/api/pick/metric/preview", methods=["POST"])
    def pick_metric_preview() -> Response:
        out = _capture_grasp6d_plan()
        _save_grasp6d_run({"mode": "preview", "at": time.time(), **out})
        return jsonify(out), (200 if out.get("ok") else 422)

    @app.route("/api/pick/metric/calibration/sample", methods=["POST"])
    def pick_metric_calibration_sample() -> Response:
        try:
            frame = wrist_rgbd.capture_aligned(median_frames=2)
        except Exception as exc:
            return jsonify({"ok": False, "reason": "wrist_rgbd_capture_failed", "detail": str(exc)}), 503
        marker = grasp6d.detect_calibration_marker(frame.color_bgr, frame.intrinsics)
        if not marker.get("ok"):
            return jsonify(marker), 422
        feedback = service.read_servo_deg(fast=True)
        raw = feedback.get("servo_deg")
        if not feedback.get("ok") or not isinstance(raw, list) or len(raw) < 6:
            return jsonify({"ok": False, "reason": "arm_feedback_unavailable", "feedback": feedback}), 503
        import numpy as np

        T_base_tool = grasp6d.fk_tool_transform(np.radians(np.asarray(raw[:6], dtype=float)))
        out = grasp6d.append_handeye_sample(T_base_tool, np.asarray(marker["T_camera_target"], dtype=float))
        out["marker"] = marker
        out["rgbd"] = frame.public_info()
        return jsonify(out)

    @app.route("/api/pick/metric/calibration/build", methods=["POST"])
    def pick_metric_calibration_build() -> Response:
        out = grasp6d.build_handeye_calibration(grasp6d.list_handeye_samples())
        return jsonify(out), (200 if out.get("ok") else 422)

    @app.route("/api/pick/metric/calibration", methods=["GET", "DELETE"])
    def pick_metric_calibration_status() -> Response:
        if request.method == "DELETE":
            try:
                grasp6d.HAND_EYE_SAMPLES_PATH.unlink(missing_ok=True)
            except OSError as exc:
                return jsonify({"ok": False, "reason": "sample_reset_failed", "detail": str(exc)}), 500
        cal = grasp6d.load_calibration()
        cal.pop("T_tool_camera_np", None)
        return jsonify(
            {
                "ok": True,
                "sample_count": len(grasp6d.list_handeye_samples()),
                "calibration": cal,
            }
        )

    @app.route("/api/pick/snapshot", methods=["POST"])
    def pick_snapshot() -> Response:
        out = pick_vision.capture_and_detect()
        _apply_pick_detection_to_preset(out)
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    @app.route("/api/pick/detect", methods=["POST"])
    def pick_detect() -> Response:
        body = request.get_json(silent=True) or {}
        if body.get("capture_if_missing", True):
            out = pick_vision.capture_and_detect()
        else:
            out = pick_vision.detect_on_latest_snapshot(capture_if_missing=False)
        _apply_pick_detection_to_preset(out)
        feedback = service.read_servo_deg(fast=True)
        if feedback.get("ok") and feedback.get("servo_deg"):
            out["grasp_estimate"] = _scan_grasp_estimate(feedback["servo_deg"], out)
            out["scan_feedback"] = feedback
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    def _pick_scene_jpeg() -> Response:
        path = pick_vision.scene_overlay_path()
        if not path.is_file():
            return jsonify({"ok": False, "reason": "no_scene_overlay"}), 404
        return send_file(path, mimetype="image/jpeg", max_age=0)

    @app.route("/api/pick/diagnostic")
    def pick_diagnostic() -> Response:
        import sys

        from go2_dashboard.paths import PROJECT_ROOT

        cap = orbbec_capture.capture_orbbec_jpeg()
        det_out: dict[str, Any] = {"ok": False, "reason": "capture_failed"}
        if cap.get("ok"):
            scripts_dir = str(PROJECT_ROOT / "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            from box_object_detector import detector_status

            det_out = pick_vision.detect_on_latest_snapshot(capture_if_missing=False)
            det_out["detector_status"] = detector_status()
        return jsonify(
            {
                "ok": cap.get("ok", False),
                "capture": cap,
                "detection": det_out,
                "preset": pick_preset.preset_info(),
            }
        )

    @app.route("/api/pick/scene.jpg")
    def pick_scene_jpeg() -> Response:
        return _pick_scene_jpeg()

    @app.route("/api/pick/detect.jpg")
    def pick_detect_jpeg() -> Response:
        return _pick_scene_jpeg()

    def _execute_legacy_grasp_approach() -> tuple[dict[str, Any], int]:
        found = program_store.find_scan_waypoint()
        if found is None:
            return {"ok": False, "reason": "scan_waypoint_not_found"}, 404
        _program_id, wp = found
        raw = wp.get("servo_deg")
        if not isinstance(raw, list):
            return {"ok": False, "reason": "invalid_scan_waypoint"}, 400
        scan_sd = service.clamp_servo_deg([float(x) for x in raw[:7]])
        preset = pick_preset.load_preset()
        off = pick_preset.effective_joint_offsets(
            last_detection=preset.get("last_detection"),
        )
        if off is None:
            return (
                {
                    "ok": False,
                    "reason": "grasp_preset_missing",
                    "hint": "Calibrazione zero, offset programma o foto normale prima di Presa oggetto",
                },
                404,
            )
        target = pick_preset.grasp_servo_approach_from_scan(
            scan_sd,
            offsets=off,
            last_detection=preset.get("last_detection"),
        )
        if target is None:
            return {"ok": False, "reason": "grasp_target_invalid"}, 400
        transit = _safe_transit_target()
        service._halt_cartesian_stream(wait_idle=True)
        program_runner.clear_stop_request()
        couple = service.ensure_coupled_for_motion()
        if not couple.get("ok"):
            return couple, 502
        transit_move = None
        if transit is not None:
            transit_move = program_runner.move_to_servo_deg_smooth(transit)
            if not (transit_move.get("ok") or transit_move.get("skipped")):
                transit_move["phase"] = "transit_zero"
                transit_move["target_servo_deg"] = transit
                transit_move["coupling"] = couple
                return (
                    {
                        "ok": False,
                        "reason": transit_move.get("reason", "transit_failed"),
                        "preset": "grasp_approach",
                        "coupling": couple,
                        "transit_zero": transit,
                        "transit_move": transit_move,
                        "scan_servo_deg": scan_sd,
                    },
                    502,
                )
        open_j6 = pick_preset.gripper_open_j6_deg(scan_sd)
        out = program_runner.move_to_servo_deg_smooth(target, pin_joints={6: open_j6})
        out["preset"] = "grasp_approach"
        out["gripper_open_deg"] = open_j6
        out["gripper_closed_deg"] = pick_preset.gripper_close_j6_deg(scan_sd)
        out["coupling"] = couple
        out["transit_zero"] = transit
        out["transit_move"] = transit_move
        out["scan_servo_deg"] = scan_sd
        out["joint_offset_deg"] = off
        out["has_zero_calibration"] = bool(preset.get("zero_calibration"))
        zc = preset.get("zero_calibration") or {}
        ref_vis = zc.get("vision_at_scan") if isinstance(zc, dict) else None
        ld = preset.get("last_detection")
        dpx = pick_preset._vision_pixel_delta(
            ref_vis if isinstance(ref_vis, dict) else None,
            ld if isinstance(ld, dict) else None,
        )
        if dpx is not None:
            out["vision_pixel_delta"] = [round(dpx[0], 1), round(dpx[1], 1)]
        d_orient = pick_preset._vision_orientation_delta_deg(
            ref_vis if isinstance(ref_vis, dict) else None,
            ld if isinstance(ld, dict) else None,
        )
        if d_orient is not None:
            out["vision_orientation_delta_deg"] = d_orient
            out["orient_joint_index"] = pick_preset._orient_joint_index()
        zc_dict = zc if isinstance(zc, dict) else None
        if zc_dict and isinstance(ld, dict):
            scan_sd = zc_dict.get("scan_servo_deg")
            base_off = preset.get("joint_offset_deg")
            j5i = pick_preset._orient_joint_index()
            if (
                isinstance(scan_sd, list)
                and len(scan_sd) > j5i
                and isinstance(base_off, list)
                and len(base_off) > j5i
            ):
                out["j5_breakdown"] = pick_preset._j5_target_breakdown(
                    scan_j5=float(scan_sd[j5i]),
                    base_off_j5=float(base_off[j5i]),
                    zc=zc_dict,
                    data=preset,
                    cur_dict=ld,
                )
        manual_orient = preset.get("manual_orient_offset_deg")
        if manual_orient is not None:
            out["manual_orient_offset_deg"] = float(manual_orient)
        out["joint_offset_deg_effective"] = off
        out["target_servo_deg"] = target
        try:
            from go2_dashboard.d1_jog import pick_teach_model

            if pick_teach_model.model_is_active(preset):
                _moff, blend = pick_teach_model.effective_offsets_from_model(
                    preset.get("last_detection"),
                    data=preset,
                )
                out["has_teach_model"] = True
                out["teach_model_blend"] = blend
                if isinstance(blend, dict):
                    out["teach_interp_method"] = blend.get("method")
                    out["teach_nearest_id"] = blend.get("nearest_id")
        except Exception:
            pass
        return out, (200 if out.get("ok") else 502)

    def _execute_grasp6d_attempt(*, dry_run: bool = False, pregrasp_only: bool = False) -> dict[str, Any]:
        import numpy as np

        run: dict[str, Any] = {
            "ok": False,
            "mode": "dry_run" if dry_run else ("pregrasp_only" if pregrasp_only else "grasp6d"),
            "started_at": time.time(),
            "steps": [],
            "daemon_invariant": "external_hold_only",
        }

        def step(name: str, payload: dict[str, Any]) -> None:
            run["steps"].append({"name": name, **payload})

        initial = _capture_grasp6d_plan()
        step("plan", initial)
        if not initial.get("ok"):
            run["reason"] = initial.get("reason", "plan_failed")
            run["finished_at"] = time.time()
            _save_grasp6d_run(run)
            return run
        run["plan"] = initial
        if dry_run:
            run.update({"ok": True, "finished_at": time.time()})
            _save_grasp6d_run(run)
            return run

        couple = service.ensure_coupled_for_motion()
        step("couple", couple)
        if not (couple.get("ok") or couple.get("skipped")):
            run["reason"] = couple.get("reason", "couple_failed")
            run["finished_at"] = time.time()
            _save_grasp6d_run(run)
            return run

        open_deg = pick_preset.gripper_open_j6_deg()
        close_deg = pick_preset.gripper_close_j6_deg()
        latest = initial
        for attempt_index in range(2):
            run["attempt"] = attempt_index + 1
            selected = (latest.get("plan") or {}).get("selected") or {}
            pre = ((selected.get("pregrasp") or {}).get("servo_deg"))
            if not isinstance(pre, list):
                run["reason"] = "pregrasp_target_missing"
                break
            pre = service.clamp_servo_deg([float(x) for x in pre[:7]])
            pre[6] = open_deg
            moved = program_runner.move_to_servo_deg_smooth(pre, pin_joints={6: open_deg})
            step("pregrasp", {"attempt": attempt_index + 1, **moved})
            if not moved.get("ok"):
                run["reason"] = moved.get("reason", "pregrasp_failed")
                break

            # Tre osservazioni consecutive: nessun movimento finale se la posa oscilla.
            observations: list[dict[str, Any]] = []
            for _ in range(3):
                obs = _capture_grasp6d_plan()
                step("realign_observation", {"attempt": attempt_index + 1, "ok": obs.get("ok"), "reason": obs.get("reason")})
                if not obs.get("ok"):
                    observations = []
                    break
                observations.append(obs)
            if len(observations) != 3:
                run["reason"] = "realign_not_stable"
                latest = _capture_grasp6d_plan()
                continue
            target_ts = [
                np.asarray((((o.get("plan") or {}).get("selected") or {}).get("T_base_grasp")), dtype=float)
                for o in observations
            ]
            centers = np.stack([T[:3, 3] for T in target_ts])
            spread_m = float(np.max(np.linalg.norm(centers - np.mean(centers, axis=0), axis=1)))
            rotations = [T[:3, :3] for T in target_ts]
            rot_spread_deg = 0.0
            for R in rotations[1:]:
                c = float(np.clip((np.trace(R @ rotations[0].T) - 1.0) * 0.5, -1.0, 1.0))
                rot_spread_deg = max(rot_spread_deg, math.degrees(math.acos(c)))
            stable = spread_m <= 0.008 and rot_spread_deg <= 5.0
            step(
                "realign_gate",
                {"attempt": attempt_index + 1, "ok": stable, "spread_m": spread_m, "rotation_spread_deg": rot_spread_deg},
            )
            if not stable:
                run["reason"] = "realign_not_stable"
                latest = observations[-1]
                continue
            latest = observations[-1]
            if pregrasp_only:
                run.update({"ok": True, "reason": None, "finished_at": time.time()})
                _save_grasp6d_run(run)
                return run
            selected = (latest.get("plan") or {}).get("selected") or {}
            grasp_target = ((selected.get("grasp") or {}).get("servo_deg"))
            if not isinstance(grasp_target, list):
                run["reason"] = "grasp_target_missing"
                break
            grasp_target = service.clamp_servo_deg([float(x) for x in grasp_target[:7]])
            grasp_target[6] = open_deg
            approached = program_runner.move_to_servo_deg_smooth(grasp_target, pin_joints={6: open_deg})
            step("approach", {"attempt": attempt_index + 1, **approached})
            if not approached.get("ok"):
                run["reason"] = approached.get("reason", "approach_failed")
                break

            closed_target = list(grasp_target)
            closed_target[6] = close_deg
            closed = program_runner.move_to_servo_deg_smooth(closed_target)
            step("close", {"attempt": attempt_index + 1, **closed})
            if not closed.get("ok"):
                run["reason"] = closed.get("reason", "close_failed")
                break

            grasp_T = np.asarray(selected["T_base_grasp"], dtype=float)
            lift_T = grasp_T.copy()
            lift_T[2, 3] += float(os.environ.get("D1_GRASP6D_LIFT_M", "0.09"))
            lift_ik = grasp6d.ik_pose(lift_T, primary_seed=np.radians(np.asarray(closed_target[:6], dtype=float)))
            if not lift_ik.get("ok"):
                step("lift", lift_ik)
                run["reason"] = lift_ik.get("reason", "lift_ik_failed")
                break
            lift_target = service.clamp_servo_deg(list(lift_ik["servo_deg"]))
            lift_target[6] = close_deg
            lifted = program_runner.move_to_servo_deg_smooth(lift_target, pin_joints={6: close_deg})
            step("lift", {"attempt": attempt_index + 1, **lifted})
            if not lifted.get("ok"):
                run["reason"] = lifted.get("reason", "lift_failed")
                break

            fb = service.read_servo_deg(fast=True)
            actual_j6 = float(fb["servo_deg"][6]) if fb.get("ok") and isinstance(fb.get("servo_deg"), list) else None
            gripper_blocked = actual_j6 is not None and actual_j6 > close_deg + float(
                os.environ.get("D1_GRASP6D_GRIPPER_BLOCK_DELTA_DEG", "2.5")
            )
            post = _capture_grasp6d_plan()
            floor_absent = not post.get("ok") and post.get("reason") in {
                "no_cluster_above_floor",
                "object_component_not_found",
                "object_cluster_too_small",
                "no_safe_6d_grasp_candidate",
            }
            moved_with_lift = False
            if post.get("ok"):
                before_box = np.asarray((latest.get("plan") or {}).get("T_base_box"), dtype=float)
                after_box = np.asarray((post.get("plan") or {}).get("T_base_box"), dtype=float)
                if before_box.shape == (4, 4) and after_box.shape == (4, 4):
                    moved_with_lift = float(after_box[2, 3] - before_box[2, 3]) >= 0.04
            verified = bool(gripper_blocked and (floor_absent or moved_with_lift))
            verify = {
                "ok": verified,
                "actual_gripper_deg": actual_j6,
                "closed_empty_deg": close_deg,
                "gripper_blocked": gripper_blocked,
                "floor_position_absent": floor_absent,
                "box_moved_with_lift": moved_with_lift,
            }
            step("verify", verify)
            if verified:
                run.update({"ok": True, "reason": None, "verification": verify, "finished_at": time.time()})
                _save_grasp6d_run(run)
                return run

            run["reason"] = "grasp_not_verified"
            # Recovery controllato: torna al pregrasp e riapre, senza mai fare release.
            recovered = program_runner.move_to_servo_deg_smooth(pre, pin_joints={6: close_deg})
            step("retract", {"attempt": attempt_index + 1, **recovered})
            if not recovered.get("ok"):
                break
            opened = list(pre)
            opened[6] = open_deg
            opened_out = program_runner.move_to_servo_deg_smooth(opened)
            step("reopen", {"attempt": attempt_index + 1, **opened_out})
            latest = _capture_grasp6d_plan()
            if not latest.get("ok"):
                break

        # Qualunque uscita fallita mantiene l'ultima posa comandata in HOLD.
        hold = service.hold_pose_stream(servo_deg=service.get_servo_cache())
        step("safe_hold", hold)
        run["finished_at"] = time.time()
        _save_grasp6d_run(run)
        return run

    @app.route("/api/pick/grasp/goto", methods=["POST"])
    def pick_grasp_goto() -> Response:
        body = request.get_json(silent=True) or {}
        if os.environ.get("D1_GRASP6D_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}:
            if not body.get("dry_run") and not body.get("pregrasp_only") and body.get("confirm") != "EXECUTE_GRASP6D":
                return jsonify(
                    {
                        "ok": False,
                        "reason": "explicit_grasp6d_confirmation_required",
                        "required_confirm": "EXECUTE_GRASP6D",
                    }
                ), 409
            out = _execute_grasp6d_attempt(
                dry_run=bool(body.get("dry_run")),
                pregrasp_only=bool(body.get("pregrasp_only")),
            )
            code = 200 if out.get("ok") else 422
            return jsonify(out), code
        out, code = _execute_legacy_grasp_approach()
        return jsonify(out), code

    @app.route("/api/pick/grasp_legacy/preview", methods=["POST"])
    def pick_grasp_legacy_preview() -> Response:
        out = pick_vision.capture_and_detect()
        _apply_pick_detection_to_preset(out)
        feedback = service.read_servo_deg(fast=True)
        if feedback.get("ok") and feedback.get("servo_deg"):
            out["grasp_estimate"] = _scan_grasp_estimate(feedback["servo_deg"], out)
            out["scan_feedback"] = feedback
        out["mode"] = "legacy"
        return jsonify(out), (200 if out.get("ok") else 502)

    @app.route("/api/pick/grasp_legacy/execute", methods=["POST"])
    def pick_grasp_legacy_execute() -> Response:
        out, code = _execute_legacy_grasp_approach()
        out["mode"] = "legacy"
        return jsonify(out), code

    @app.route("/api/pick/grasp6d/status", methods=["GET"])
    def pick_grasp6d_status() -> Response:
        enabled = os.environ.get("D1_GRASP6D_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
        cal = grasp6d.load_calibration()
        cal.pop("T_tool_camera_np", None)
        out = {
            "ok": True,
            "enabled": enabled,
            "rgbd": wrist_rgbd.health(capture=False),
            "sample_count": len(grasp6d.list_handeye_samples()),
            "calibration": cal,
        }
        return jsonify(out)

    @app.route("/api/pick/grasp6d/preview", methods=["POST"])
    def pick_grasp6d_preview() -> Response:
        out = _capture_grasp6d_plan()
        _save_grasp6d_run({"mode": "preview_api", "at": time.time(), **out})
        return jsonify(out), (200 if out.get("ok") else 422)

    @app.route("/api/pick/grasp6d/pregrasp", methods=["POST"])
    def pick_grasp6d_pregrasp() -> Response:
        out = _execute_grasp6d_attempt(pregrasp_only=True)
        return jsonify(out), (200 if out.get("ok") else 422)

    @app.route("/api/pick/grasp6d/execute", methods=["POST"])
    def pick_grasp6d_execute() -> Response:
        body = request.get_json(silent=True) or {}
        if body.get("confirm") != "EXECUTE_GRASP6D":
            return jsonify({"ok": False, "reason": "explicit_grasp6d_confirmation_required", "required_confirm": "EXECUTE_GRASP6D"}), 409
        out = _execute_grasp6d_attempt(pregrasp_only=False)
        return jsonify(out), (200 if out.get("ok") else 422)

    def _pick_gripper_move(j6_target: float, *, action: str) -> tuple[Response, int]:
        fb = service.read_servo_deg(fast=True)
        if not fb.get("ok") or not fb.get("servo_deg"):
            return jsonify({"ok": False, "reason": fb.get("reason", "no_feedback")}), 502
        cur = list(fb["servo_deg"])
        target = service.clamp_servo_deg(cur[:7])
        target[6] = round(float(j6_target), 3)
        service._halt_cartesian_stream(wait_idle=True)
        program_runner.clear_stop_request()
        couple = service.ensure_coupled_for_motion()
        if not couple.get("ok"):
            return jsonify(couple), 502
        out = program_runner.move_to_servo_deg_smooth(target)
        out["action"] = action
        out["gripper_target_deg"] = target[6]
        out["target_servo_deg"] = target
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    @app.route("/api/pick/gripper/open", methods=["POST"])
    def pick_gripper_open() -> Response:
        found = program_store.find_scan_waypoint()
        if found is None:
            return jsonify({"ok": False, "reason": "scan_waypoint_not_found"}), 404
        _program_id, wp = found
        raw = wp.get("servo_deg")
        if not isinstance(raw, list):
            return jsonify({"ok": False, "reason": "invalid_scan_waypoint"}), 400
        scan_sd = service.clamp_servo_deg([float(x) for x in raw[:7]])
        open_j6 = pick_preset.gripper_open_j6_deg(scan_sd)
        resp, code = _pick_gripper_move(open_j6, action="gripper_open")
        return resp, code

    @app.route("/api/pick/gripper/close", methods=["POST"])
    def pick_gripper_close() -> Response:
        found = program_store.find_scan_waypoint()
        if found is None:
            return jsonify({"ok": False, "reason": "scan_waypoint_not_found"}), 404
        _program_id, wp = found
        raw = wp.get("servo_deg")
        if not isinstance(raw, list):
            return jsonify({"ok": False, "reason": "invalid_scan_waypoint"}), 400
        scan_sd = service.clamp_servo_deg([float(x) for x in raw[:7]])
        close_j6 = pick_preset.gripper_close_j6_deg(scan_sd)
        resp, code = _pick_gripper_move(close_j6, action="gripper_close")
        return resp, code

    @app.route("/api/presets/scan", methods=["GET"])
    def preset_scan_info() -> Response:
        found = program_store.find_scan_waypoint()
        if found is None:
            return jsonify({"ok": False, "reason": "scan_waypoint_not_found"}), 404
        program_id, wp = found
        return jsonify(
            {
                "ok": True,
                "program_id": program_id,
                "waypoint": wp,
                "servo_deg": wp.get("servo_deg"),
            }
        )

    @app.route("/api/presets/scan/goto", methods=["POST"])
    def preset_scan_goto() -> Response:
        body = request.get_json(silent=True) or {}
        variant = str(body.get("variant") or "base").strip().lower()
        if variant in ("j90_left", "left", "sx"):
            side_target = _scan_side_target("left")
            out = _move_via_safe_transit(side_target)
            out["preset"] = "scan"
            out["scan_variant"] = "j90_left"
            out["waypoint_name"] = "Punto SCANSIONE 90 SX"
            out["target_servo_deg"] = side_target
            return jsonify(out), (200 if out.get("ok") else 502)
        if variant in ("j90_right", "right", "dx"):
            side_target = _scan_side_target("right")
            out = _move_via_safe_transit(side_target)
            out["preset"] = "scan"
            out["scan_variant"] = "j90_right"
            out["waypoint_name"] = "Punto SCANSIONE 90 DX"
            out["target_servo_deg"] = side_target
            return jsonify(out), (200 if out.get("ok") else 502)
        if variant not in ("base", "j90", "90"):
            variant = "base"
        scan_variant = "j90" if variant in ("j90", "90") else "base"
        found = program_store.find_scan_waypoint(variant=scan_variant)
        if found is None:
            reason = "scan_j90_waypoint_not_found" if scan_variant == "j90" else "scan_waypoint_not_found"
            return jsonify({"ok": False, "reason": reason}), 404
        _program_id, wp = found
        raw = wp.get("servo_deg")
        if not isinstance(raw, list) or len(raw) < 6:
            return jsonify({"ok": False, "reason": "invalid_waypoint"}), 400
        servo = service.clamp_servo_deg([float(x) for x in raw[:7]])
        out = _move_via_safe_transit(servo)
        out["preset"] = "scan"
        out["scan_variant"] = scan_variant
        out["waypoint_name"] = wp.get("name")
        out["target_servo_deg"] = servo
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    @app.route("/api/programs/<program_id>/waypoints/<waypoint_id>/goto", methods=["POST"])
    def programs_goto_waypoint(program_id: str, waypoint_id: str) -> Response:
        prog = program_store.load_program(program_id)
        if prog is None:
            return jsonify({"ok": False, "reason": "program_not_found"}), 404
        wp = next((w for w in (prog.get("waypoints") or []) if w.get("id") == waypoint_id), None)
        if wp is None:
            return jsonify({"ok": False, "reason": "waypoint_not_found"}), 404
        sd = wp.get("servo_deg")
        if not isinstance(sd, list):
            return jsonify({"ok": False, "reason": "invalid_waypoint"}), 400
        service._halt_cartesian_stream(wait_idle=True)
        program_runner.clear_stop_request()
        out = program_runner.move_to_servo_deg_smooth(sd)
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    @app.route("/api/programs/<program_id>/run", methods=["POST"])
    def programs_run(program_id: str) -> Response:
        service._halt_cartesian_stream(wait_idle=True)
        out = program_runner.run_program(program_id)
        code = 200 if out.get("ok") else 409
        return jsonify(out), code

    @app.route("/api/programs/run/status")
    def programs_run_status() -> Response:
        return jsonify({"ok": True, "status": program_runner.execution_status()})

    @app.route("/api/programs/run/stop", methods=["POST"])
    def programs_run_stop() -> Response:
        return jsonify(program_runner.request_stop())

    @app.route("/api/cartesian/move", methods=["POST"])
    def cartesian_move() -> Response:
        body = request.get_json(silent=True) or {}
        servo_deg, err = _servo_deg_from_body(body)
        if servo_deg is None:
            return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
        try:
            dx = float(body.get("dx_m", body.get("dx", 0)))
            dy = float(body.get("dy_m", body.get("dy", 0)))
            dz = float(body.get("dz_m", body.get("dz", 0)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "dx/dy/dz must be numeric (meters)"}), 400
        out = cartesian.cartesian_move_delta(
            servo_deg,
            dx_m=dx,
            dy_m=dy,
            dz_m=dz,
            interpolated=bool(body.get("interpolated", True)),
        )
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    _mount_motor_health(app)
    # Hermes torna nel processo principale :5056, come nel focus dashboard.
    os.environ.setdefault("GO2_HERMES_INTEGRATED", "1")
    os.environ.setdefault("HERMES_D1_JOG_URL", "http://127.0.0.1:5056")
    from go2_dashboard.hermes.routes import bp as hermes_bp

    app.register_blueprint(hermes_bp)
    # Il daemon deve esistere già all'avvio: health non può dichiarare sano un
    # servizio che possiede solo il file binario.
    daemon_started = service.ensure_command_daemon()
    auto_enable = os.environ.get("D1_JOG_AUTO_ENABLE", "1").strip().lower() in {
        "1", "true", "yes", "on"
    }
    if daemon_started and auto_enable and service.binaries_status().get("real_arm"):
        feedback = service.read_servo_deg(fast=True)
        if feedback.get("ok") and feedback.get("servo_deg"):
            atomic_hold = service.couple_and_hold_pose(feedback["servo_deg"], force=True)
            startup_arm_stabilization.update(
                {
                    "ok": bool(atomic_hold.get("ok") or atomic_hold.get("skipped")),
                    "reason": "stabilized",
                    "feedback": feedback,
                    "coupling_hold": atomic_hold,
                }
            )
        else:
            startup_arm_stabilization.update({"ok": False, "reason": "no_feedback", "feedback": feedback})
    elif not daemon_started:
        startup_arm_stabilization.update({"ok": False, "reason": "daemon_start_failed"})
    else:
        startup_arm_stabilization.update({"ok": True, "skipped": True, "reason": "auto_enable_disabled"})
    return app

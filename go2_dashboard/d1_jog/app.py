"""Flask app — pagina jog D1 con slider (SDK)."""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context
from werkzeug.exceptions import HTTPException

from go2_dashboard.d1_jog import (
    cartesian,
    jog_stream,
    orbbec_capture,
    pick_preset,
    pick_vision,
    program_runner,
    program_store,
    service,
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


def create_d1_jog_app() -> Flask:
    template_dir = PROJECT_ROOT / "templates"
    static_dir = PROJECT_ROOT / "static"
    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir) if static_dir.is_dir() else None,
        static_url_path="/static",
    )

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
            "dashboard_port": int(os.environ.get("D1_JOG_PORT", os.environ.get("GO2_DASHBOARD_PORT", "5053"))),
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

    @app.route("/program")
    def program_editor() -> str:
        return render_template("d1_program_editor.html", **_page_ctx(dash_mode="arm"))

    @app.route("/api/motion/status")
    def motion_status() -> Response:
        return jsonify(service.motion_status())

    @app.route("/api/motion/reset", methods=["POST"])
    def motion_reset() -> Response:
        return jsonify(service.motion_reset())

    @app.route("/api/health")
    def health() -> Response:
        st = service.binaries_status()
        return jsonify(
            {
                "ok": st["command_ok"] and st["feedback_ok"],
                "service": "d1_jog_dashboard",
                "started_at": _PROCESS_STARTED,
                "binaries": st,
                "dds_domain": int(os.environ.get("D1_DDS_DOMAIN", os.environ.get("GO2_DDS_DOMAIN", "0"))),
                "dds_interface": (os.environ.get("GO2_DDS_INTERFACE") or os.environ.get("D1_DDS_INTERFACE") or "eth0"),
                "cyclonedds_uri_set": bool((os.environ.get("CYCLONEDDS_URI") or "").strip()),
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
        left_default = [87.1, 19.2, 26.0, 0.1, 37.8, 0.4, 5.0]
        right_default = [-87.1, 19.2, 26.0, 0.1, 37.8, 0.4, 5.0]
        if side == "left":
            return _parse_servo_env("D1_SCAN_LEFT_DEG", left_default)
        return _parse_servo_env("D1_SCAN_RIGHT_DEG", right_default)

    def _front_camera_jpeg() -> bytes | None:
        try:
            import cv2
        except ImportError:
            return None
        idx = int(os.environ.get("D1_FRONT_V4L_INDEX", "10"))
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            return None
        try:
            ok, frame = cap.read()
        finally:
            cap.release()
        if not ok or frame is None:
            return None
        ok_enc, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok_enc or buf is None:
            return None
        return buf.tobytes()

    @app.route("/api/front/last.jpg")
    def front_last_jpeg() -> Response:
        jpg = _front_camera_jpeg()
        if jpg is None:
            return Response("front camera unavailable", status=503)
        return Response(jpg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.route("/api/front/live.mjpg")
    def front_live_mjpg() -> Response:
        period = float(os.environ.get("D1_FRONT_MJPEG_PERIOD_S", "0.10"))

        def generate():
            last: bytes | None = None
            while True:
                jpg = _front_camera_jpeg()
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
            stream_with_context(generate()),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
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
                for i in range(20):
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
        service._halt_cartesian_stream(wait_idle=True)
        couple = service.ensure_coupled_for_motion()
        if not couple.get("ok"):
            return jsonify({"ok": False, "reason": "couple_failed", "coupling": couple}), 502
        move = service.jog_pose_deg(target, mode=1)
        move["action"] = move.get("action") or "scan_side_jog"
        if not move.get("ok"):
            move["coupling"] = couple
            move["target_servo_deg"] = target
            move["scan_side"] = side
            return jsonify(move), 502
        detect = pick_vision.capture_and_detect()
        _apply_pick_detection_to_preset(detect)
        ok_all = bool(move.get("ok")) and bool(detect.get("ok"))
        return jsonify(
            {
                "ok": ok_all,
                "scan_side": side,
                "target_servo_deg": target,
                "coupling": couple,
                "move": move,
                "detection": detect,
            }
        ), (200 if ok_all else 502)

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
        out = service.motor_release()
        out["funcode"] = 5
        out["action"] = "motor_release"
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/joints/zero", methods=["POST"])
    def joints_zero() -> Response:
        out = service.go_zero()
        code = 200 if out.get("ok") or out.get("skipped") else 502
        return jsonify(out), code

    @app.route("/api/arm/status")
    def arm_status() -> Response:
        return jsonify({"ok": True, "arm_coupled": service.arm_coupled()})

    @app.route("/api/arm/couple", methods=["POST"])
    def arm_couple() -> Response:
        body = request.get_json(silent=True) or {}
        out = service.ensure_coupled(
            with_power=bool(body.get("with_power")),
            force=bool(body.get("force")),
        )
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
        return Response(
            stream_with_context(orbbec_capture.generate_rgb_mjpeg_stream()),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    @app.route("/api/orbbec/last.jpg")
    def orbbec_last_jpeg() -> Response:
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

    @app.route("/api/pick/grasp/goto", methods=["POST"])
    def pick_grasp_goto() -> Response:
        found = program_store.find_scan_waypoint()
        if found is None:
            return jsonify({"ok": False, "reason": "scan_waypoint_not_found"}), 404
        _program_id, wp = found
        raw = wp.get("servo_deg")
        if not isinstance(raw, list):
            return jsonify({"ok": False, "reason": "invalid_scan_waypoint"}), 400
        scan_sd = service.clamp_servo_deg([float(x) for x in raw[:7]])
        preset = pick_preset.load_preset()
        off = pick_preset.effective_joint_offsets(
            last_detection=preset.get("last_detection"),
        )
        if off is None:
            return jsonify(
                {
                    "ok": False,
                    "reason": "grasp_preset_missing",
                    "hint": "Calibrazione zero, offset programma o foto normale prima di Presa oggetto",
                }
            ), 404
        target = pick_preset.grasp_servo_approach_from_scan(
            scan_sd,
            offsets=off,
            last_detection=preset.get("last_detection"),
        )
        if target is None:
            return jsonify({"ok": False, "reason": "grasp_target_invalid"}), 400
        service._halt_cartesian_stream(wait_idle=True)
        couple = service.ensure_coupled_for_motion()
        if not couple.get("ok"):
            return jsonify(couple), 502
        open_j6 = pick_preset.gripper_open_j6_deg(scan_sd)
        out = program_runner.move_to_servo_deg_smooth(target, pin_joints={6: open_j6})
        out["preset"] = "grasp_approach"
        out["gripper_open_deg"] = open_j6
        out["gripper_closed_deg"] = pick_preset.gripper_close_j6_deg(scan_sd)
        out["coupling"] = couple
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
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

    def _pick_gripper_move(j6_target: float, *, action: str) -> tuple[Response, int]:
        fb = service.read_servo_deg(fast=True)
        if not fb.get("ok") or not fb.get("servo_deg"):
            return jsonify({"ok": False, "reason": fb.get("reason", "no_feedback")}), 502
        cur = list(fb["servo_deg"])
        target = service.clamp_servo_deg(cur[:7])
        target[6] = round(float(j6_target), 3)
        service._halt_cartesian_stream(wait_idle=True)
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
            service._halt_cartesian_stream(wait_idle=True)
            couple = service.ensure_coupled_for_motion()
            if not couple.get("ok"):
                return jsonify(couple), 502
            out = service.jog_pose_deg(side_target, mode=1)
            out["preset"] = "scan"
            out["coupling"] = couple
            out["scan_variant"] = "j90_left"
            out["waypoint_name"] = "Punto SCANSIONE 90 SX"
            out["target_servo_deg"] = side_target
            return jsonify(out), (200 if out.get("ok") else 502)
        if variant in ("j90_right", "right", "dx"):
            side_target = _scan_side_target("right")
            service._halt_cartesian_stream(wait_idle=True)
            couple = service.ensure_coupled_for_motion()
            if not couple.get("ok"):
                return jsonify(couple), 502
            out = service.jog_pose_deg(side_target, mode=1)
            out["preset"] = "scan"
            out["coupling"] = couple
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
        service._halt_cartesian_stream(wait_idle=True)
        couple = service.ensure_coupled_for_motion()
        if not couple.get("ok"):
            return jsonify(couple), 502
        out = program_runner.move_to_servo_deg_smooth(servo)
        out["preset"] = "scan"
        out["coupling"] = couple
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

    return app

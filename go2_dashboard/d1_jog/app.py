"""Flask app — pagina jog D1 con slider (SDK)."""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

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

    from go2_dashboard.blueprints.d1_pick_teach import bp as d1_pick_teach_bp
    app.register_blueprint(d1_pick_teach_bp)

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

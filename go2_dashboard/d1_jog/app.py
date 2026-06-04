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
        return jsonify(
            {
                "ok": chosen is not None,
                "probe_order": order,
                "nodes": nodes,
                "chosen_v4l_index": chosen,
                "rgb_only": orbbec_capture._rgb_only(),
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

    @app.route("/api/pick/calibrate/zero/finish", methods=["POST"])
    def pick_calibrate_zero_finish() -> Response:
        """Chiude calibrazione: coppia ON (fine task) + offset + riferimento visione."""
        body = request.get_json(silent=True) or {}
        vis = body.get("vision_at_scan")
        if body.get("servo_deg"):
            servo, err = _servo_deg_from_body(body)
            if servo is None:
                return jsonify({"ok": False, "reason": err or "no_feedback"}), 503
            out = pick_preset.save_zero_calibration(
                servo,
                vision_at_scan=vis if isinstance(vis, dict) else None,
            )
        else:
            out = pick_preset.finish_zero_calibration_after_release(
                vision_at_scan=vis if isinstance(vis, dict) else None,
            )
        code = 200 if out.get("ok") else 502
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
        target = pick_preset.grasp_servo_from_scan(
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
        out = program_runner.move_to_servo_deg_smooth(target)
        out["preset"] = "grasp"
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
        out["joint_offset_deg_effective"] = off
        out["target_servo_deg"] = target
        code = 200 if out.get("ok") else 502
        return jsonify(out), code

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
        try:
            j0_delta = float(body.get("j0_delta_deg", 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "j0_delta_deg_invalid"}), 400
        found = program_store.find_scan_waypoint()
        if found is None:
            return jsonify({"ok": False, "reason": "scan_waypoint_not_found"}), 404
        _program_id, wp = found
        raw = wp.get("servo_deg")
        if not isinstance(raw, list) or len(raw) < 6:
            return jsonify({"ok": False, "reason": "invalid_waypoint"}), 400
        servo = [float(x) for x in raw[:7]]
        while len(servo) < 7:
            servo.append(servo[-1])
        servo[0] = round(servo[0] + j0_delta, 3)
        servo = service.clamp_servo_deg(servo)
        service._halt_cartesian_stream(wait_idle=True)
        couple = service.ensure_coupled_for_motion()
        if not couple.get("ok"):
            return jsonify(couple), 502
        out = program_runner.move_to_servo_deg_smooth(servo)
        out["preset"] = "scan"
        out["coupling"] = couple
        out["j0_delta_deg"] = j0_delta
        out["waypoint_name"] = wp.get("name")
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

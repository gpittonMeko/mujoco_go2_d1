"""Focused dashboard: Pick teach, Go2 base pose, Hermes."""

from __future__ import annotations

import os

from typing import Any

from flask import Flask, Response, jsonify, render_template, request

from go2_dashboard.blueprints.d1_pick_teach import bp as d1_pick_teach_bp
from go2_dashboard.blueprints.grasp import bp as grasp_bp
from go2_dashboard.blueprints.operator_api import bp as operator_api_bp
from go2_dashboard.hermes.routes import bp as hermes_bp
from go2_dashboard.operator_stack import go2_local
from go2_dashboard.d1_jog import service as d1_service
from go2_dashboard.paths import PROJECT_ROOT


def _mount_motor_health(app: Flask) -> None:
    """Mount the Go2 motor health dashboard into the focus Flask process."""
    from go2_dashboard.motor_health_app import create_motor_health_app
    from go2_dashboard.motor_health_env import apply_motor_health_env_defaults, ensure_thermal_settings_file

    apply_motor_health_env_defaults()
    ensure_thermal_settings_file()
    motor_app = create_motor_health_app()
    for rule in motor_app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        path = "/focus/motor" if rule.rule == "/" else rule.rule
        if rule.rule == "/api/health":
            path = "/api/motor/health"
        endpoint = f"motor_health.{rule.endpoint}"
        methods = sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})
        app.add_url_rule(path, endpoint, motor_app.view_functions[rule.endpoint], methods=methods)


def _assets_version() -> str:
    newest = 0.0
    for rel in ("static/focus", "static/hermes", "static"):
        root = PROJECT_ROOT / rel
        if not root.is_dir():
            continue
        for path in root.glob("*.*"):
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                pass
    return str(int(newest)) if newest > 0 else "1"


def create_focus_app() -> Flask:
    os.environ.setdefault("GO2_HERMES_INTEGRATED", "1")
    if os.environ.get("GO2_HERMES_STANDALONE", "0").strip().lower() in {"1", "true", "yes", "on"}:
        os.environ["GO2_HERMES_STANDALONE"] = "0"

    app = Flask(
        "go2_focus_dashboard",
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
        static_url_path="/static",
    )

    @app.route("/")
    @app.route("/focus")
    @app.route("/focus/")
    def focus_index() -> Response:
        port = int(os.environ.get("GO2_FOCUS_PORT", os.environ.get("GO2_DASHBOARD_PORT", "5056")))
        script_root = (os.environ.get("GO2_FOCUS_URL_PREFIX") or request.script_root or "").strip().rstrip("/")
        html = render_template(
            "focus_dashboard.html",
            dashboard_port=port,
            go2_host=os.environ.get("GO2_HOST", "192.168.123.18").strip(),
            go2_local=go2_local(),
            script_root=script_root,
            asset_ver=_assets_version(),
        )
        return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})

    @app.route("/focus/teach")
    def focus_teach() -> Response:
        script_root = (os.environ.get("GO2_FOCUS_URL_PREFIX") or request.script_root or "").strip().rstrip("/")
        html = render_template(
            "focus_teach.html",
            script_root=script_root,
            asset_ver=_assets_version(),
        )
        return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})

    @app.route("/api/arm/status")
    def focus_arm_status() -> Response:
        return jsonify({"ok": True, "arm_coupled": d1_service.arm_coupled()})

    @app.route("/focus/pick")
    def focus_pick() -> Response:
        port = int(os.environ.get("GO2_FOCUS_PORT", os.environ.get("GO2_DASHBOARD_PORT", "5056")))
        html = render_template(
            "d1_jog_dashboard.html",
            dashboard_port=port,
            d1_arm_host=os.environ.get("D1_ARM_HOST", os.environ.get("SERVO_ARM_HOST", "192.168.123.100")),
            go2_local=os.environ.get("GO2_LOCAL", "0"),
            dash_mode="arm",
            embed_in_operators=True,
            go2_host=os.environ.get("GO2_HOST", "192.168.123.18").strip(),
        )
        return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})

    @app.route("/focus/hermes")
    def focus_hermes() -> Response:
        port = int(os.environ.get("GO2_FOCUS_PORT", os.environ.get("GO2_DASHBOARD_PORT", "5056")))
        script_root = (os.environ.get("GO2_FOCUS_URL_PREFIX") or request.script_root or "").strip().rstrip("/")
        html = render_template(
            "hermes.html",
            port=port,
            embed_in_operators=True,
            script_root=script_root,
        )
        return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})

    _mount_motor_health(app)

    @app.route("/api/focus/status")
    def focus_status() -> Response:
        """Small status payload for the focus UI.

        This deliberately treats AnyGrasp/AWS and depth as optional: the focus
        teach flow uses the local RealSense camera cache plus D1 teach samples.
        """
        payload: dict[str, Any] = {
            "ok": True,
            "service": "go2_focus_dashboard",
            "go2_local": go2_local(),
            "camera": {"logical_0": None, "logical_6": None},
            "arm": {"ok": None},
            "teach": {"ok": None},
            "optional": {
                "anygrasp_aws_required": False,
                "depth_required_for_manual_teach": False,
            },
        }
        try:
            from go2_dashboard.cameras import CAMERA_CACHE

            CAMERA_CACHE.start(0)
            CAMERA_CACHE.start(6)
            cams = CAMERA_CACHE.stats()
            if isinstance(cams, dict):
                payload["camera"]["logical_0"] = cams.get("0") or cams.get(0)
                payload["camera"]["logical_6"] = cams.get("6") or cams.get(6)
        except Exception as exc:  # noqa: BLE001
            payload["camera_error"] = repr(exc)
        try:
            from go2_dashboard.d1_jog import service

            payload["arm"] = {"ok": True, "arm_coupled": service.arm_coupled()}
        except Exception as exc:  # noqa: BLE001
            payload["arm"] = {"ok": False, "error": repr(exc)}
        try:
            from go2_dashboard.d1_jog import pick_teach_model

            info = pick_teach_model.list_teach_samples()
            payload["teach"] = {
                "ok": bool(info.get("ok", True)),
                "count": info.get("count"),
                "has_active_model": info.get("has_active_model"),
                "model_active": bool(info.get("has_active_model")),
            }
        except Exception as exc:  # noqa: BLE001
            payload["teach"] = {"ok": False, "error": repr(exc)}
        return jsonify(payload)

    @app.route("/api/focus/debug/log", methods=["POST"])
    def focus_debug_log() -> Response:
        """NDJSON debug da UI teach (sessione agente)."""
        import json
        import time

        body = request.get_json(silent=True) or {}
        row: dict[str, Any] = {
            "sessionId": str(body.get("sessionId") or "7c69a6"),
            "timestamp": int(body.get("timestamp") or time.time() * 1000),
            "location": str(body.get("location") or "focus_teach.js"),
            "message": str(body.get("message") or "event"),
            "data": body.get("data") if isinstance(body.get("data"), dict) else {"raw": body.get("data")},
            "hypothesisId": str(body.get("hypothesisId") or "BTN"),
        }
        if body.get("runId"):
            row["runId"] = body["runId"]
        try:
            log_path = PROJECT_ROOT / "debug-7c69a6.log"
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return jsonify({"ok": True})

    app.register_blueprint(operator_api_bp)
    app.register_blueprint(d1_pick_teach_bp)
    app.register_blueprint(grasp_bp)
    app.register_blueprint(hermes_bp)
    return app

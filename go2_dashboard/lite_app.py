"""Flask factory: dashboard operator (porta default 5052) — **senza** mount del monolite ``diagnostics_dashboard``."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, Response, render_template_string, request

from go2_dashboard.blueprints.grasp import bp as grasp_bp
from go2_dashboard.blueprints.meta import bp as meta_bp
from go2_dashboard.blueprints.operator_api import bp as operator_api_bp
from go2_dashboard.paths import PROJECT_ROOT


def create_operators_app(*, import_legacy_first: bool = False) -> Flask:
    """Solo blueprint operator + grasp + meta; route HTTP da ``operator_api``."""
    del import_legacy_first  # compat firma; il monolite non viene più importato.

    static_root = PROJECT_ROOT / "static"
    app = Flask(
        "go2_operators_dashboard",
        static_folder=str(static_root) if static_root.is_dir() else None,
        static_url_path="/static",
    )
    app.register_blueprint(meta_bp)
    app.register_blueprint(operator_api_bp)
    app.register_blueprint(grasp_bp)

    template_path = PROJECT_ROOT / "templates" / "dashboard_operators.html"

    @app.route("/")
    def operators_index() -> Response:
        url_prefix = os.environ.get("GO2_DASHBOARD_URL_PREFIX", "").strip().rstrip("/")
        script_root = url_prefix or ((request.script_root or "").rstrip("/"))
        try:
            template_text = template_path.read_text(encoding="utf-8")
        except OSError as exc:
            return Response(
                f"<!doctype html><html><body><pre>Template mancante: {template_path}\n{exc!r}</pre></body></html>",
                status=500,
                mimetype="text/html",
            )
        port = int(os.environ.get("GO2_DASHBOARD_PORT", "5052"))
        go2_host = os.environ.get("GO2_HOST", "192.168.123.18").strip()
        go2_local = "1" if os.environ.get("GO2_LOCAL", "0").lower() in {"1", "true", "yes", "on"} else "0"
        try:
            app.jinja_env.cache.clear()
        except Exception:
            pass
        html = render_template_string(
            template_text,
            go2_host=go2_host,
            dashboard_port=port,
            go2_local=go2_local,
            script_root=script_root,
        )
        return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})

    return app

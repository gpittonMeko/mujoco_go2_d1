"""Flask app for operator dashboard (default port 5052); no diagnostics_dashboard."""

from __future__ import annotations

import os

from flask import Flask, Response, render_template, request

from go2_dashboard.blueprints.grasp import bp as grasp_bp
from go2_dashboard.blueprints.meta import bp as meta_bp
from go2_dashboard.blueprints.operator_api import bp as operator_api_bp
from go2_dashboard.operator_stack import go2_local
from go2_dashboard.paths import PROJECT_ROOT


def create_operators_app() -> Flask:
    static_root = PROJECT_ROOT / "static"
    templates_dir = PROJECT_ROOT / "templates"
    app = Flask(
        "go2_operators_dashboard",
        template_folder=str(templates_dir),
        static_folder=str(static_root) if static_root.is_dir() else None,
        static_url_path="/static",
    )
    app.register_blueprint(meta_bp)
    app.register_blueprint(operator_api_bp)
    app.register_blueprint(grasp_bp)

    @app.route("/")
    def operators_index() -> Response:
        url_prefix = os.environ.get("GO2_DASHBOARD_URL_PREFIX", "").strip().rstrip("/")
        script_root = url_prefix or ((request.script_root or "").rstrip("/"))
        port = int(os.environ.get("GO2_DASHBOARD_PORT", "5052"))
        go2_host = os.environ.get("GO2_HOST", "192.168.123.18").strip()
        try:
            app.jinja_env.cache.clear()
        except Exception:
            pass
        html = render_template(
            "dashboard_operators.html",
            go2_host=go2_host,
            dashboard_port=port,
            script_root=script_root,
            go2_local=go2_local(),
        )
        return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})

    return app

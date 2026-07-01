"""Flask app for operator dashboard (default port 5052); no diagnostics_dashboard."""

from __future__ import annotations

import os

from flask import Flask, Response, render_template, request

from go2_dashboard.blueprints.d1_pick_teach import bp as d1_pick_teach_bp
from go2_dashboard.blueprints.grasp import bp as grasp_bp
from go2_dashboard.blueprints.meta import bp as meta_bp
from go2_dashboard.blueprints.operator_api import bp as operator_api_bp
from go2_dashboard.hermes.routes import bp as hermes_integrated_bp
from go2_dashboard.operator_stack import go2_local
from go2_dashboard.paths import PROJECT_ROOT


def _assets_version() -> str:
    """Versione cache-busting: mtime più recente di static/js e static/css.

    Cambia ad ogni deploy → il browser ricarica JS/CSS senza hard-refresh manuale.
    """
    newest = 0.0
    try:
        for sub in ("static/js", "static/css", "static"):
            d = PROJECT_ROOT / sub
            if not d.is_dir():
                continue
            for f in d.glob("*.*"):
                try:
                    m = f.stat().st_mtime
                    if m > newest:
                        newest = m
                except OSError:
                    continue
    except Exception:
        pass
    return str(int(newest)) if newest > 0 else "1"


def create_operators_app() -> Flask:
    os.environ.setdefault("GO2_HERMES_INTEGRATED", "1")
    # Mai GO2_HERMES_STANDALONE su :5052 — il blueprint Hermes non deve rubare GET /.
    if os.environ.get("GO2_HERMES_STANDALONE", "0").strip().lower() in {"1", "true", "yes", "on"}:
        os.environ["GO2_HERMES_STANDALONE"] = "0"

    static_root = PROJECT_ROOT / "static"
    templates_dir = PROJECT_ROOT / "templates"
    app = Flask(
        "go2_operators_dashboard",
        template_folder=str(templates_dir),
        static_folder=str(static_root) if static_root.is_dir() else None,
        static_url_path="/static",
    )

    @app.route("/")
    @app.route("/operators")
    @app.route("/operators/")
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
            asset_ver=_assets_version(),
        )
        return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})

    @app.route("/operators/d1-pick")
    def operators_d1_pick_embed() -> Response:
        """UI pick teach Orbbec (Luca) embedded nella dashboard 5052 — stesse API /api/pick/*."""
        port = int(os.environ.get("GO2_DASHBOARD_PORT", "5052"))
        go2_host = os.environ.get("GO2_HOST", "192.168.123.18").strip()
        html = render_template(
            "d1_jog_dashboard.html",
            dashboard_port=port,
            d1_arm_host=os.environ.get("D1_ARM_HOST", os.environ.get("SERVO_ARM_HOST", "192.168.123.100")),
            go2_local=os.environ.get("GO2_LOCAL", "0"),
            dash_mode="arm",
            embed_in_operators=True,
            go2_host=go2_host,
        )
        return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})

    @app.route("/operators/hermes")
    def operators_hermes_embed() -> Response:
        """Hermes (Luca) integrato su :5052 — chat/voce/visione, stesse API /api/hermes/chat."""
        port = int(os.environ.get("GO2_DASHBOARD_PORT", "5052"))
        url_prefix = os.environ.get("GO2_DASHBOARD_URL_PREFIX", "").strip().rstrip("/")
        script_root = url_prefix or ((request.script_root or "").rstrip("/"))
        html = render_template(
            "hermes.html",
            port=port,
            embed_in_operators=True,
            script_root=script_root,
        )
        return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})

    app.register_blueprint(meta_bp)
    app.register_blueprint(operator_api_bp)
    app.register_blueprint(grasp_bp)
    app.register_blueprint(d1_pick_teach_bp)
    app.register_blueprint(hermes_integrated_bp)

    return app

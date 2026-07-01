"""Flask factory — solo dashboard Hermes (chat + voce), porta 5054."""

from __future__ import annotations

import os

from flask import Flask, render_template

from go2_dashboard.hermes.routes import bp as hermes_bp
from go2_dashboard.paths import PROJECT_ROOT


def create_hermes_app() -> Flask:
    # Stesso layout di lite_app (:5052): asset in static/hermes/* → URL /static/hermes/hermes.css
    static_root = PROJECT_ROOT / "static"
    app = Flask(
        "hermes",
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(static_root) if static_root.is_dir() else None,
        static_url_path="/static",
    )
    app.register_blueprint(hermes_bp)

    @app.route("/")
    def hermes_standalone_index() -> str:
        port = int(os.environ.get("HERMES_PORT", os.environ.get("GO2_HERMES_PORT", "5054")))
        return render_template(
            "hermes.html",
            port=port,
            embed_in_operators=False,
            script_root="",
        )

    return app

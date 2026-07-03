"""Factory legacy Hermes; in produzione il blueprint gira nella dashboard 5056."""

from __future__ import annotations

from flask import Flask

from go2_dashboard.hermes.routes import bp as hermes_bp
from go2_dashboard.paths import PROJECT_ROOT


def create_hermes_app() -> Flask:
    static_root = PROJECT_ROOT / "static" / "hermes"
    app = Flask(
        "hermes",
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(static_root) if static_root.is_dir() else None,
        static_url_path="/static",
    )
    app.register_blueprint(hermes_bp)
    return app

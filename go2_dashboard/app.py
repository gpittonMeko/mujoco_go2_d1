"""Flask application factory for the modular dashboard server."""

from __future__ import annotations

from flask import Flask

from go2_dashboard.blueprints.meta import bp as meta_bp
from go2_dashboard.legacy_mount import mount_diagnostics_dashboard_routes


def create_modular_app(*, import_legacy_first: bool = True) -> Flask:
    """Stesse route di ``diagnostics_dashboard.APP`` + ``/api/modular/*``. ``import_legacy_first=False`` se ``diagnostics_dashboard`` è già importato."""
    if import_legacy_first:
        import diagnostics_dashboard  # noqa: F401

    app = Flask(
        "go2_dashboard",
        static_folder=None,
        template_folder=None,
    )
    app.register_blueprint(meta_bp)
    mount_diagnostics_dashboard_routes(app)
    return app

"""Copia le URL rule di ``diagnostics_dashboard.APP`` su un'altra Flask app."""

from __future__ import annotations

from flask import Flask


def mount_diagnostics_dashboard_routes(target: Flask, *, legacy_module: str = "diagnostics_dashboard") -> None:
    """Registra su ``target`` le stesse route (e CORS) di ``diagnostics_dashboard.APP``."""
    import importlib

    dd = importlib.import_module(legacy_module)
    legacy_app = dd.APP

    for fn in legacy_app.after_request_funcs.get(None, []) or []:
        target.after_request(fn)

    for rule in legacy_app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        view_func = legacy_app.view_functions.get(rule.endpoint)
        if view_func is None:
            continue
        target.add_url_rule(
            rule.rule,
            endpoint=rule.endpoint,
            view_func=view_func,
            methods=rule.methods,
            strict_slashes=rule.strict_slashes,
        )

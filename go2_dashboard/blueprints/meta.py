"""/api/modular/* (shared with modular server)."""

from __future__ import annotations

import os
from typing import Any

from flask import Blueprint, jsonify

bp = Blueprint("go2_dashboard_meta", __name__, url_prefix="/api/modular")


@bp.route("/info", methods=["GET"])
def modular_info() -> Any:
    return jsonify({"ok": True, "pid": os.getpid()})

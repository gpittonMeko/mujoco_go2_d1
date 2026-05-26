"""Operator dashboard REST API (split to avoid a single huge module)."""
from __future__ import annotations

import logging
import os
import sys
import time

from flask import Blueprint, g, request

bp = Blueprint("go2_operator_api", __name__)
_PROCESS_STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

_timing_log = logging.getLogger("go2_dashboard.operator_api.timing")


@bp.before_request
def _operator_api_timing_start() -> None:
    g._operator_api_t0 = time.perf_counter()


@bp.after_request
def _operator_api_timing_finish(resp):
    t0 = getattr(g, "_operator_api_t0", None)
    if t0 is None:
        return resp
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    try:
        resp.headers["X-Dashboard-Server-Ms"] = f"{elapsed_ms:.2f}"
    except Exception:
        pass
    flag = (os.environ.get("GO2_HTTP_TIMING_LOG") or "1").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        line = f"HTTP_TIMING {request.method} {request.path} {elapsed_ms:.2f}ms"
        try:
            print(line, file=sys.stderr, flush=True)
        except Exception:
            pass
        try:
            _timing_log.info("%s", line)
        except Exception:
            pass
    return resp


from . import routes  # noqa: E402, F401

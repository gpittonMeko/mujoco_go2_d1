"""API grasp operator (AnyGrasp): stub + proxy opzionale verso worker HTTP locale."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from flask import Blueprint, jsonify, request

bp = Blueprint("go2_dashboard_grasp", __name__, url_prefix="/api/grasp")


def _worker_base() -> str:
    return (os.environ.get("GO2_ANYGRASP_WORKER_URL") or "http://127.0.0.1:8765").strip().rstrip("/")


def _proxy_json(
    method: str, path: str, body: dict[str, Any] | None = None, timeout_s: float = 60.0
) -> tuple[dict[str, Any], int]:
    url = _worker_base() + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None and method.upper() != "GET":
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {"ok": True}, resp.getcode() or 200
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(err_body) if err_body.strip() else {"ok": False, "reason": str(exc)}
        except json.JSONDecodeError:
            payload = {"ok": False, "reason": str(exc), "body": err_body[:800]}
        return payload, exc.code
    except Exception as exc:
        return {"ok": False, "reason": "worker_unreachable", "detail": repr(exc), "worker_url": url}, 503


@bp.route("/health", methods=["GET"])
def grasp_health() -> Any:
    """Stato integrazione AnyGrasp: worker opzionale + env."""
    worker = _worker_base()
    use_proxy = os.environ.get("GO2_ANYGRASP_PROXY", "1").lower() in {"1", "true", "yes"}
    out: dict[str, Any] = {
        "ok": True,
        "mode": "stub",
        "worker_url": worker,
        "proxy_enabled": use_proxy,
        "checkpoint_env": (os.environ.get("GO2_ANYGRASP_CHECKPOINT") or "").strip() or None,
    }
    if use_proxy:
        proxied, code = _proxy_json("GET", "/health", timeout_s=3.0)
        out["worker_reachable"] = code < 500
        out["worker_payload"] = proxied
        out["mode"] = "proxy" if out["worker_reachable"] else "stub"
    else:
        out["worker_reachable"] = False
        out["hint_it"] = (
            "Imposta GO2_ANYGRASP_WORKER_URL e avvia il worker AnyGrasp; "
            "GO2_ANYGRASP_PROXY=0 per solo stub."
        )
    return jsonify(out)


@bp.route("/plan", methods=["POST"])
def grasp_plan() -> Any:
    body = request.get_json(silent=True) or {}
    if os.environ.get("GO2_ANYGRASP_PROXY", "1").lower() in {"1", "true", "yes"}:
        payload, code = _proxy_json("POST", "/plan", body=body, timeout_s=120.0)
        return jsonify(payload), code
    return (
        jsonify(
            {
                "ok": False,
                "reason": "anygrasp_worker_not_configured",
                "hint_it": "Avvia il worker AnyGrasp e GO2_ANYGRASP_PROXY=1 (default) oppure implementa piano locale.",
            }
        ),
        503,
    )


@bp.route("/execute", methods=["POST"])
def grasp_execute() -> Any:
    body = request.get_json(silent=True) or {}
    if os.environ.get("GO2_ANYGRASP_PROXY", "1").lower() in {"1", "true", "yes"}:
        payload, code = _proxy_json("POST", "/execute", body=body, timeout_s=120.0)
        return jsonify(payload), code
    return (
        jsonify(
            {
                "ok": False,
                "reason": "anygrasp_worker_not_configured",
                "hint_it": "Worker AnyGrasp non raggiungibile — solo stub in dashboard operator.",
            }
        ),
        503,
    )

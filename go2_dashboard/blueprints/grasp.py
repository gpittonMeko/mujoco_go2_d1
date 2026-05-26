"""API grasp operator: proxy verso worker HTTP (OpenVLA / AWS / RTX locale)."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from flask import Blueprint, jsonify, request

from go2_dashboard.operator_plan_cache import set_last_grasp_plan

bp = Blueprint("go2_dashboard_grasp", __name__, url_prefix="/api/grasp")


def _robot_jpeg(device: int) -> bytes | None:
    """JPEG da CameraCache senza importare operator_api (evita ciclo con routes)."""
    try:
        from go2_dashboard.cameras import CAMERA_CACHE
        from go2_dashboard.operator_stack import go2_local

        if go2_local():
            return CAMERA_CACHE.get_jpeg(device)
    except Exception:
        pass
    return None


def _worker_base() -> str:
    return (os.environ.get("GO2_ANYGRASP_WORKER_URL") or "http://127.0.0.1:8765").strip().rstrip("/")


def _proxy_enabled() -> bool:
    return os.environ.get("GO2_ANYGRASP_PROXY", "1").lower() in {"1", "true", "yes", "on"}


def _cloud_mode() -> bool:
    if os.environ.get("GO2_GRASP_CLOUD_MODE", "0").lower() in {"1", "true", "yes", "on"}:
        return True
    base = _worker_base()
    try:
        host = (urllib.parse.urlparse(base).hostname or "").strip()
    except Exception:
        return False
    if not host or host in {"127.0.0.1", "localhost"}:
        return False
    if host.startswith("192.168.") or host.startswith("10."):
        return False
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) >= 2:
            try:
                if 16 <= int(parts[1]) <= 31:
                    return False
            except ValueError:
                pass
    # IP pubblico / DNS AWS → invia JPEG inline
    return True


def _worker_token() -> str:
    return (os.environ.get("GO2_WORKER_TOKEN") or "").strip()


def _embed_robot_cameras(body: dict[str, Any]) -> dict[str, Any]:
    """In cloud mode la NX invia JPEG inline (Orbbec polso + RealSense front)."""
    if not _cloud_mode():
        return body
    out = dict(body)
    logical = int(out.get("logical_camera_device") or 0)
    if not out.get("jpeg_base64"):
        wrist = _robot_jpeg(0 if logical == 0 else logical)
        if wrist:
            out["jpeg_base64"] = base64.standard_b64encode(wrist).decode("ascii")
    if not out.get("jpeg_base64_front"):
        front = _robot_jpeg(6)
        if front:
            out["jpeg_base64_front"] = base64.standard_b64encode(front).decode("ascii")
    if not out.get("image_url"):
        out["image_url"] = f"embedded://camera/{logical}"
    out["cloud_embedded"] = True
    return out


def _proxy_json(
    method: str, path: str, body: dict[str, Any] | None = None, timeout_s: float = 60.0
) -> tuple[dict[str, Any], int]:
    url = _worker_base() + path
    data = None
    headers = {"Accept": "application/json"}
    token = _worker_token()
    if token:
        headers["X-Worker-Token"] = token
    if body is not None and method.upper() != "GET":
        payload = _embed_robot_cameras(body) if path == "/plan" else body
        data = json.dumps(payload).encode("utf-8")
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


def grasp_health_payload() -> dict[str, Any]:
    worker = _worker_base()
    use_proxy = _proxy_enabled()
    out: dict[str, Any] = {
        "ok": True,
        "mode": "stub",
        "worker_url": worker,
        "proxy_enabled": use_proxy,
        "cloud_mode": _cloud_mode(),
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
            "Imposta GO2_ANYGRASP_WORKER_URL e GO2_ANYGRASP_PROXY=1; "
            "GO2_GRASP_CLOUD_MODE=1 per worker AWS (JPEG inline)."
        )
    return out


@bp.route("/health", methods=["GET"])
def grasp_health() -> Any:
    return jsonify(grasp_health_payload())


def grasp_plan_via_worker(body: dict[str, Any], *, timeout_s: float = 120.0) -> tuple[dict[str, Any], int]:
    if not _proxy_enabled():
        return (
            {
                "ok": False,
                "reason": "anygrasp_worker_not_configured",
                "hint_it": "GO2_ANYGRASP_PROXY=0 — abilita proxy verso worker.",
            },
            503,
        )
    payload, code = _proxy_json("POST", "/plan", body=body, timeout_s=timeout_s)
    if code < 400 and isinstance(payload, dict) and payload.get("ok"):
        set_last_grasp_plan(payload)
    return payload, code


@bp.route("/plan", methods=["POST"])
def grasp_plan() -> Any:
    body = request.get_json(silent=True) or {}
    payload, code = grasp_plan_via_worker(body)
    return jsonify(payload), code


@bp.route("/execute", methods=["POST"])
def grasp_execute() -> Any:
    body = request.get_json(silent=True) or {}
    if _proxy_enabled():
        payload, code = _proxy_json("POST", "/execute", body=body, timeout_s=120.0)
        return jsonify(payload), code
    return (
        jsonify(
            {
                "ok": False,
                "reason": "anygrasp_worker_not_configured",
                "hint_it": "Worker grasp non raggiungibile.",
            }
        ),
        503,
    )

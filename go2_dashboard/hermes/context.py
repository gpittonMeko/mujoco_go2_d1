"""Contesto live della dashboard integrata 5056."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def operator_base() -> str:
    return (os.environ.get("HERMES_OPERATOR_URL") or d1_jog_base()).strip().rstrip("/")


def d1_jog_base() -> str:
    return (os.environ.get("HERMES_D1_JOG_URL") or "http://127.0.0.1:5056").strip().rstrip("/")


def operator_required() -> bool:
    return os.environ.get("HERMES_REQUIRE_OPERATOR", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def operator_reachable_quick(*, timeout_s: float = 2.5) -> bool:
    """True se il backend contesto configurato risponde."""
    data = _fetch_json("/api/health", timeout_s=timeout_s)
    return bool(data.get("ok"))


def hermes_capabilities() -> dict[str, Any]:
    integrated = os.environ.get("GO2_HERMES_INTEGRATED", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }
    return {
        "operator_required": operator_required(),
        "operator_url": operator_base(),
        "operator_reachable": operator_reachable_quick(),
        "d1_jog_url": d1_jog_base(),
        "sport_direct": os.environ.get("HERMES_SPORT_DIRECT", "1").strip().lower()
        not in {"0", "false", "no", "off"},
        "integrated": integrated,
        "standalone": not integrated,
    }


def _fetch_from(base: str, path: str, *, timeout_s: float = 8.0) -> dict[str, Any]:
    url = base + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        return {"ok": False, "path": path, "error": repr(exc)}


def _fetch_json(path: str, *, timeout_s: float = 8.0) -> dict[str, Any]:
    """Compatibilità per funzioni che usano il backend contesto configurato."""
    return _fetch_from(operator_base(), path, timeout_s=timeout_s)


def _fetch_jog(path: str, *, timeout_s: float = 8.0) -> dict[str, Any]:
    return _fetch_from(d1_jog_base(), path, timeout_s=timeout_s)


def build_robot_context() -> dict[str, Any]:
    health = _fetch_jog("/api/health")
    cameras = _fetch_jog("/api/cameras/rgb_status")
    grasp = _fetch_jog("/api/pick/preset")
    feedback = _fetch_jog("/api/joints/feedback", timeout_s=12.0)

    scene_summary: dict[str, Any] = {}
    if isinstance(feedback, dict):
        scene_summary = {
            "ok": feedback.get("ok"),
            "servo_deg": feedback.get("servo_deg"),
        }

    return {
        "integrated": True,
        "dashboard_port": 5056,
        "dashboard_reachable": bool(health.get("ok")),
        "operator_reachable": bool(health.get("ok")),
        "operator_required": operator_required(),
        "standalone_ok": not operator_required(),
        "cameras": cameras,
        "grasp_pipeline": grasp,
        "scene_3d": scene_summary,
        "health": health,
    }


def context_for_prompt() -> str:
    return json.dumps(build_robot_context(), ensure_ascii=False, indent=2)[:10000]

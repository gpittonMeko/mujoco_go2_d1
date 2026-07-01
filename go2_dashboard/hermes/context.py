"""Lettura contesto robot — integrato su :5052 o standalone :5054 → operator locale."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def integrated_on_operator_dashboard() -> bool:
    return os.environ.get("GO2_HERMES_INTEGRATED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def operator_base() -> str:
    return (os.environ.get("HERMES_OPERATOR_URL") or "http://127.0.0.1:5052").strip().rstrip("/")


def d1_jog_base() -> str:
    return (os.environ.get("HERMES_D1_JOG_URL") or "http://127.0.0.1:5053").strip().rstrip("/")


def operator_required() -> bool:
    return os.environ.get("HERMES_REQUIRE_OPERATOR", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def operator_reachable_quick(*, timeout_s: float = 2.5) -> bool:
    """True se la dashboard operator :5052 risponde (opzionale per Hermes)."""
    if integrated_on_operator_dashboard():
        try:
            from go2_dashboard.operator_stack import go2_local

            return go2_local()
        except Exception:
            return True
    data = _fetch_json("/api/health", timeout_s=timeout_s)
    return bool(data.get("ok"))


def hermes_capabilities() -> dict[str, Any]:
    return {
        "operator_required": operator_required(),
        "operator_url": operator_base(),
        "operator_reachable": operator_reachable_quick(),
        "d1_jog_url": d1_jog_base(),
        "sport_direct": os.environ.get("HERMES_SPORT_DIRECT", "1").strip().lower()
        not in {"0", "false", "no", "off"},
        "standalone": not operator_required(),
    }


def _fetch_json(path: str, *, timeout_s: float = 8.0) -> dict[str, Any]:
    if integrated_on_operator_dashboard():
        try:
            from flask import current_app

            resp = current_app.test_client().get(path)
            data = resp.get_json(silent=True)
            if isinstance(data, dict):
                return data
            return {"ok": False, "path": path, "error": "non_json"}
        except Exception as exc:
            return {"ok": False, "path": path, "error": repr(exc)}
    url = operator_base() + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        return {"ok": False, "path": path, "error": repr(exc)}


def build_robot_context() -> dict[str, Any]:
    cameras = _fetch_json("/api/cameras/status")
    grasp = _fetch_json("/api/arm/grasp_pipeline")
    scene = _fetch_json("/api/arm/scene_3d?fast=1", timeout_s=12.0)

    scene_summary: dict[str, Any] = {}
    if isinstance(scene, dict):
        scene_summary = {
            "ok": scene.get("ok"),
            "servo_deg": scene.get("servo_deg"),
            "grasp_display_base_link_m": scene.get("grasp_display_base_link_m"),
        }

    return {
        "operator_reachable": bool((cameras or {}).get("ok") or (grasp or {}).get("ok")),
        "operator_required": operator_required(),
        "standalone_ok": not operator_required(),
        "cameras": cameras,
        "grasp_pipeline": grasp,
        "scene_3d": scene_summary,
    }


def context_for_prompt() -> str:
    return json.dumps(build_robot_context(), ensure_ascii=False, indent=2)[:10000]

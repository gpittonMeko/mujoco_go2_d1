#!/usr/bin/env python3
"""
Worker HTTP compatibile con go2_dashboard/blueprints/grasp.py:
  GET  /health
  POST /plan
  POST /execute

Modalità (env ``GO2_GRASP_WORKER_BACKEND``):
  - ``planner`` (default): piano **reale** via ``scripts/box_grasp_planner.py`` (serve clone repo +
    JPEG da ``WORKER_CAMERA_JPG_URL`` o ``image_url`` nel JSON).
  - ``stub``: risposta fissa per test UI senza OpenCV.

OpenVLA (policy VLM) **non** è incluso qui: richiede pesi e integrazione azione→robot separata.
Il backend ``planner`` **non** importa ``diagnostics_dashboard`` / monolite: solo ``scripts/box_grasp_planner.py``.
"""
from __future__ import annotations

import os
from typing import Any

from flask import Flask, jsonify, request

app = Flask(__name__)


def _backend() -> str:
    return (os.environ.get("GO2_GRASP_WORKER_BACKEND") or "planner").strip().lower()


def _stub_plan(body: dict[str, Any] | None) -> dict[str, Any]:
    _ = body
    pts = [
        [0.42, 0.05, 0.18],
        [0.41, 0.06, 0.19],
        [0.43, 0.04, 0.17],
    ]
    return {
        "ok": True,
        "backend": "stub",
        "hint_it": "Imposta GO2_GRASP_WORKER_BACKEND=planner e WORKER_CAMERA_JPG_URL per piano reale.",
        "grasp_display_base_link_m": [0.42, 0.05, 0.18],
        "operators_grasp_points_base_link_m": pts,
        "grip_point": {"cx": 320, "cy": 240, "u": 0.5, "v": 0.5},
        "operators_overlay_points": [{"x": 0.5, "y": 0.5, "label": "stub_center"}],
    }


@app.get("/health")
def health() -> Any:
    mode = _backend()
    planner_ok = False
    planner_err = None
    if mode == "planner":
        try:
            from planner_runtime import planner_import_ok

            planner_ok, planner_err = planner_import_ok()
        except Exception as exc:
            planner_err = repr(exc)
    return jsonify(
        {
            "ok": True,
            "backend": mode,
            "planner_import_ok": planner_ok,
            "planner_import_error": planner_err,
            "camera_jpg_url": os.environ.get("WORKER_CAMERA_JPG_URL", ""),
            "hint_it": "planner=box_grasp_planner sul repo; OpenVLA non è in questo processo.",
        }
    )


@app.post("/plan")
def plan() -> Any:
    body = request.get_json(silent=True) or {}
    mode = _backend()
    if mode == "stub":
        return jsonify(_stub_plan(body))
    try:
        from planner_runtime import plan_from_http_json, planner_import_ok

        ok, err = planner_import_ok()
        if not ok:
            return (
                jsonify(
                    {
                        "ok": False,
                        "reason": "planner_import_failed",
                        "detail": err,
                        "fallback": "Imposta GO2_GRASP_WORKER_BACKEND=stub oppure installa dipendenze (opencv, numpy) e assicurati che il repo sia completo sotto REPO_ROOT.",
                    }
                ),
                503,
            )
        return jsonify(plan_from_http_json(body))
    except Exception as exc:
        return jsonify({"ok": False, "reason": "planner_exception", "detail": repr(exc)}), 500


@app.post("/execute")
def execute() -> Any:
    body = request.get_json(silent=True) or {}
    mode = _backend()
    if mode == "stub":
        return jsonify({"ok": True, "backend": "stub", "merged_preview": body})
    try:
        from planner_runtime import execute_echo

        return jsonify(execute_echo(body))
    except Exception as exc:
        return jsonify({"ok": False, "reason": "execute_exception", "detail": repr(exc)}), 500


def main() -> None:
    host = os.environ.get("WORKER_BIND_HOST", "0.0.0.0")
    port = int(os.environ.get("WORKER_PORT", "8765"))
    app.run(host=host, port=port, threaded=False)


if __name__ == "__main__":
    main()

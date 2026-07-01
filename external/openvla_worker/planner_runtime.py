"""
Piano presa reale: usa **solo** ``scripts/box_grasp_planner.py`` + ``arm_kinematics_d1_template.py``.

**Non** importa né avvia ``diagnostics_dashboard`` / ``serve_dashboard_modular`` (monolite HTTP).
È la stessa libreria geometrica già usabile da script CLI; sul PC RTX serve il clone repo per quei file
in ``scripts/`` (non è dipendenza runtime dal processo monolite).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

# .../mujoco_go2_d1/external/openvla_worker/planner_runtime.py -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _ensure_scripts_path() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from go2_dashboard.paths import ensure_d1_scripts_on_sys_path

    ensure_d1_scripts_on_sys_path()


def planner_import_ok() -> tuple[bool, str | None]:
    try:
        _ensure_scripts_path()
        import box_grasp_planner  # noqa: F401
    except Exception as exc:
        return False, repr(exc)
    return True, None


def _fetch_jpeg(url: str, timeout_s: float = 20.0) -> bytes:
    req = Request(url, headers={"User-Agent": "go2-grasp-worker/1.0"})
    with urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def _decode_bgr(jpeg_bytes: bytes) -> Any:
    import cv2
    import numpy as np

    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("cv2.imdecode fallito (JPEG non valido?)")
    return frame


def _augment_for_operator_ui(planner_out: dict[str, Any]) -> dict[str, Any]:
    """Aggiunge chiavi lette da ``static/js/operators.js`` (marker 3D / nuvola minima)."""
    out: dict[str, Any] = dict(planner_out)
    out["backend"] = "box_grasp_planner"
    prev = planner_out.get("preview") or {}
    grasp_xyz: list[float] | None = None
    if prev.get("ok"):
        for st in prev.get("plan") or []:
            if isinstance(st, dict) and st.get("stage") == "grasp":
                t = st.get("target_xyz_m")
                if isinstance(t, (list, tuple)) and len(t) >= 3:
                    grasp_xyz = [float(t[0]), float(t[1]), float(t[2])]
                    break
    tgt = planner_out.get("target") or {}
    if grasp_xyz is None and tgt.get("ok"):
        b = tgt.get("base_xyz_m")
        if isinstance(b, (list, tuple)) and len(b) >= 3:
            grasp_xyz = [float(b[0]), float(b[1]), float(b[2])]
    if grasp_xyz is not None:
        out["grasp_display_base_link_m"] = grasp_xyz
        out["translation"] = grasp_xyz
        x, y, z = grasp_xyz
        out["operators_grasp_points_base_link_m"] = [
            grasp_xyz,
            [x + 0.008, y, z],
            [x - 0.008, y, z],
            [x, y + 0.008, z],
            [x, y - 0.008, z],
        ]
    return out


def plan_from_http_json(body: dict[str, Any] | None) -> dict[str, Any]:
    """
    ``body`` può contenere:
      - ``image_url`` (str) — JPEG RGB; default da env ``WORKER_CAMERA_JPG_URL``
      - ``object_detection`` (dict) — pass-through a ``plan_from_frame``
      - ``logical_camera_device`` (int) — es. 0 polso
    """
    body = dict(body or {})
    url = (
        body.get("image_url")
        or body.get("camera_jpg_url")
        or os.environ.get("WORKER_CAMERA_JPG_URL")
        or "http://192.168.123.18:5052/api/robot/camera/0.jpg"
    )
    if not isinstance(url, str):
        return {"ok": False, "reason": "bad_image_url", "hint_it": "image_url deve essere una stringa HTTP(S) verso un JPEG."}

    _ensure_scripts_path()
    from box_grasp_planner import plan_from_frame

    try:
        raw = _fetch_jpeg(url)
        frame = _decode_bgr(raw)
    except (URLError, OSError, ValueError, TimeoutError) as exc:
        return {
            "ok": False,
            "reason": "image_fetch_failed",
            "detail": repr(exc),
            "image_url": url,
            "hint_it": "Verifica che la NX risponda su LAN e che WORKER_CAMERA_JPG_URL sia corretto.",
        }

    logical = body.get("logical_camera_device")
    if logical is not None:
        try:
            logical = int(logical)
        except (TypeError, ValueError):
            logical = None
    obj = body.get("object_detection")
    if obj is not None and not isinstance(obj, dict):
        obj = None

    raw_plan = plan_from_frame(
        frame,
        object_detection=obj,
        logical_camera_device=logical,
    )
    merged = _augment_for_operator_ui(raw_plan)
    merged["image_url_used"] = url
    merged["repo_root"] = str(REPO_ROOT)
    return merged


def execute_echo(body: dict[str, Any] | None) -> dict[str, Any]:
    """Execute non invia comandi DDS al cane: lo fa il software sulla NX. Qui solo eco + hint."""
    return {
        "ok": True,
        "backend": "box_grasp_planner_worker",
        "hint_it": (
            "Il worker fornisce solo piano geometrico (POST /plan). "
            "Esecuzione braccio: usa la **dashboard operator** (5052, ``serve_dashboard_lite``) o altro "
            "controllo che già invii comandi al D1 — **non** passa dal monolite ``diagnostics_dashboard``."
        ),
        "request_echo": body or {},
    }

"""
Piano presa reale: usa **solo** ``scripts/box_grasp_planner.py`` + ``arm_kinematics_d1_template.py``.

**Non** importa né avvia ``diagnostics_dashboard`` / ``serve_dashboard_modular`` (monolite HTTP).
È la stessa libreria geometrica già usabile da script CLI; sul PC RTX serve il clone repo per quei file
in ``scripts/`` (non è dipendenza runtime dal processo monolite).
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

def _repo_root() -> Path:
    raw = (os.environ.get("GO2_REPO_ROOT") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent.parent


REPO_ROOT = _repo_root()
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _ensure_scripts_path() -> None:
    s = str(SCRIPTS_DIR)
    if s not in sys.path:
        sys.path.insert(0, s)


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
    _ensure_scripts_path()
    from box_grasp_planner import plan_from_frame

    image_source = "url"
    url_used: str | None = None
    try:
        b64 = body.get("jpeg_base64") or body.get("image_jpeg_b64")
        if isinstance(b64, str) and b64.strip():
            raw = base64.standard_b64decode(b64.strip())
            frame = _decode_bgr(raw)
            image_source = "jpeg_base64"
        else:
            url = (
                body.get("image_url")
                or body.get("camera_jpg_url")
                or os.environ.get("WORKER_CAMERA_JPG_URL")
                or ""
            )
            if not isinstance(url, str) or not url.strip() or url.startswith("embedded://"):
                return {
                    "ok": False,
                    "reason": "no_image",
                    "hint_it": "Serve jpeg_base64 dalla NX (GO2_GRASP_CLOUD_MODE=1) o image_url HTTP verso un JPEG.",
                }
            url_used = url.strip()
            raw = _fetch_jpeg(url_used)
            frame = _decode_bgr(raw)
    except (URLError, OSError, ValueError, TimeoutError) as exc:
        return {
            "ok": False,
            "reason": "image_fetch_failed",
            "detail": repr(exc),
            "image_url": url_used,
            "hint_it": "Verifica JPEG inline (cloud) o URL camera raggiungibile dal worker.",
        }

    logical = body.get("logical_camera_device")
    if logical is not None:
        try:
            logical = int(logical)
        except (TypeError, ValueError):
            logical = None

    depth_frame = None
    depth_b64 = body.get("depth_jpeg_b64")
    if isinstance(depth_b64, str) and depth_b64.strip():
        try:
            from rgbd_pointcloud import decode_depth_bgr

            depth_frame = decode_depth_bgr(base64.standard_b64decode(depth_b64.strip()))
            image_source = f"{image_source}+depth"
        except Exception:
            depth_frame = None

    depth_scale = body.get("depth_scale_m_per_unit")
    if depth_scale is None:
        raw_scale = (os.environ.get("GO2_DEPTH_SCALE_M_PER_UNIT") or "").strip()
        if raw_scale:
            try:
                depth_scale = float(raw_scale)
            except ValueError:
                depth_scale = None
    else:
        try:
            depth_scale = float(depth_scale)
        except (TypeError, ValueError):
            depth_scale = None

    obj = body.get("object_detection")
    if obj is not None and not isinstance(obj, dict):
        obj = None
    if obj is None or not obj.get("ok"):
        try:
            from box_object_detector import detect_box_object

            obj = detect_box_object(frame)
            instr = str(body.get("instruction") or body.get("task") or "").strip()
            if instr and isinstance(obj, dict):
                obj["instruction"] = instr
        except Exception:
            obj = obj if isinstance(obj, dict) else None

    raw_plan = plan_from_frame(
        frame,
        object_detection=obj,
        logical_camera_device=logical,
        depth_frame=depth_frame,
        depth_scale_m_per_unit=depth_scale,
    )
    merged = _augment_for_operator_ui(raw_plan)
    merged["backend"] = "box_grasp_planner"
    merged["image_source"] = image_source
    merged["image_url_used"] = url_used or body.get("image_url")
    merged["repo_root"] = str(_repo_root())
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

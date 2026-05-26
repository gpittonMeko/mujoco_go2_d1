from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

from flask import request

from go2_dashboard.cameras import CAMERA_CACHE, CAMERA_DEVICES
from go2_dashboard.paths import PROJECT_ROOT
from go2_dashboard.operator_stack import go2_local

try:
    import cv2
except Exception:
    cv2 = None
try:
    import numpy as np
except Exception:
    np = None  # type: ignore[misc, assignment]

def _mission_env_safe() -> dict[str, Any]:
    keys = (
        "GO2_DASHBOARD_PORT",
        "GO2_DASHBOARD_BIND",
        "GO2_LOCAL",
        "GO2_ENABLE_BASE_MOTION",
        "GO2_ENABLE_REAL_ARM",
        "GO2_ENABLE_ARM_PLAN_EXECUTE",
        "GO2_ENABLE_ARM_ESTOP_HTTP",
        "GO2_ENABLE_HERMES_AGENT",
        "GO2_HERMES_MODEL",
        "GO2_HERMES_DEFAULT_CAMERA",
        "GO2_DDS_DOMAIN",
        "GO2_DDS_INTERFACE",
        "GO2_DASHBOARD_PUBLIC_BASE",
        "GO2_DASHBOARD_URL_PREFIX",
    )
    return {k: (os.environ.get(k) or "").strip() or None for k in keys}


def _mission_public_base_and_prefix(body: dict[str, Any]) -> tuple[str, str]:
    prefix = (os.environ.get("GO2_DASHBOARD_URL_PREFIX") or "").strip().rstrip("/")
    raw = (
        body.get("dashboard_public_base")
        or os.environ.get("GO2_DASHBOARD_PUBLIC_BASE")
        or ""
    )
    base = str(raw).strip().rstrip("/")
    if not base:
        try:
            base = (request.url_root or "").rstrip("/")
        except Exception:
            base = ""
    return base, prefix


def _mission_run_box_detect_step(dev: int) -> dict[str, Any]:
    if not go2_local() or cv2 is None or np is None:
        return {"ok": False, "reason": "requires_nx_cv2_numpy"}
    if dev not in CAMERA_DEVICES:
        return {"ok": False, "reason": "camera_not_allowed"}
    CAMERA_CACHE.start(dev)
    jpg = CAMERA_CACHE.get_jpeg(dev, wait_s=2.5)
    if not jpg:
        return {"ok": False, "reason": "no_frame", "logical_camera": dev}
    buf = np.frombuffer(jpg, dtype=np.uint8)
    frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if frame is None:
        return {"ok": False, "reason": "jpeg_decode_failed"}
    s_scripts = str(PROJECT_ROOT / "scripts")
    if s_scripts not in sys.path:
        sys.path.insert(0, s_scripts)
    try:
        from box_object_detector import detect_box_object, detector_status
    except Exception as exc:
        return {"ok": False, "reason": "detector_import_failed", "detail": repr(exc)}
    det = detect_box_object(frame)
    st = detector_status()
    slim = {k: st.get(k) for k in ("model_path", "model_exists", "model_family", "training_scope", "classic_fallback_enabled")}
    return {"ok": True, "logical_camera": dev, "detection": det, "detector_status": slim}

def _mission_restart_instructions() -> dict[str, Any]:
    token_set = bool((os.environ.get("GO2_MISSION_ADMIN_TOKEN") or "").strip())
    return {
        "ssh_hint_it": (
            "Da PC di lab (stessa LAN): ssh unitree@<IP_NX> — poi il supervisore "
            "``nx_dashboard_supervise.sh`` rilancia ``serve_dashboard_lite.py`` se il processo termina."
        ),
        "soft_kill_example_it": (
            "Sulla NX: ``pkill -f serve_dashboard_lite.py`` — il loop di supervise riparte dopo GO2_DASHBOARD_RESTART_DELAY_S."
        ),
        "dashboard_restart_api_enabled": token_set,
        "dashboard_restart_api_hint_it": (
            "POST /api/mission/dashboard_restart con header ``X-Mission-Token`` uguale a ``GO2_MISSION_ADMIN_TOKEN`` "
            "sulla NX (imposta l'env prima di avviare la dashboard)."
            if token_set
            else "Imposta GO2_MISSION_ADMIN_TOKEN sulla NX per abilitare POST /api/mission/dashboard_restart."
        ),
    }


def _mission_worker_summary(grasp: dict[str, Any]) -> dict[str, Any]:
    proxy = grasp.get("proxy_enabled", True)
    reachable = bool(grasp.get("worker_reachable"))
    wp = grasp.get("worker_payload") if isinstance(grasp.get("worker_payload"), dict) else {}
    impl = wp.get("implementation") if isinstance(wp, dict) else None
    backend = wp.get("backend") if isinstance(wp, dict) else None
    return {
        "proxy_enabled": proxy,
        "worker_reachable": reachable,
        "worker_implementation": impl,
        "worker_backend": backend,
        "ok_for_plan": (not proxy) or reachable,
    }

def _mission_admin_token_matches() -> bool:
    exp = (os.environ.get("GO2_MISSION_ADMIN_TOKEN") or "").strip()
    if not exp:
        return False
    got = (request.headers.get("X-Mission-Token") or "").strip()
    if got and got == exp:
        return True
    body = request.get_json(silent=True) or {}
    return str(body.get("token", "")).strip() == exp


def _nx_dashboard_delayed_pkill() -> None:
    time.sleep(0.6)
    try:
        subprocess.run(
            ["pkill", "-f", "serve_dashboard_lite.py"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        print(f"[mission] dashboard_restart pkill failed: {exc!r}")

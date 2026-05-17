"""Stato stack NX / DDS per dashboard operator (subset del monolite)."""

from __future__ import annotations

import importlib.util
import os
import platform
import sys
from pathlib import Path
from typing import Any

from go2_dashboard.cameras import CAMERA_CACHE
from go2_dashboard.paths import PROJECT_ROOT


def go2_local() -> bool:
    return os.environ.get("GO2_LOCAL", "0").lower() in {"1", "true", "yes", "on"}


def command_stack_status() -> dict[str, Any]:
    try:
        modules: dict[str, Any] = {}
        for name in ("cyclonedds", "cyclonedds.idl", "unitree_sdk2py"):
            try:
                spec = importlib.util.find_spec(name)
                modules[name] = {"ok": bool(spec), "origin": None if spec is None else spec.origin}
            except Exception as exc:
                modules[name] = {"ok": False, "error": repr(exc)}
        sdk_python_ok = modules.get("cyclonedds", {}).get("ok") and modules.get("unitree_sdk2py", {}).get("ok")
        helper_pub = PROJECT_ROOT / "bin" / "d1_arm_command"
        helper_fb = PROJECT_ROOT / "bin" / "d1_arm_feedback_helper"
        d1_binaries_ok = helper_pub.exists() and helper_fb.exists()
        stack_any_ok = bool(sdk_python_ok or d1_binaries_ok)
        return {
            "ok": stack_any_ok,
            "python_dds_sdk_ok": bool(sdk_python_ok),
            "python_cyclonedds_required_for_d1_arm": False,
            "arm_motion_note": "D1 arm: subprocess → bin/d1_arm_command / d1_arm_feedback_helper.",
            "real_arm_enabled": os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() in {"1", "true", "yes"},
            "dds_domain": int(os.environ.get("GO2_DDS_DOMAIN", "0")),
            "dds_interface": os.environ.get("GO2_DDS_INTERFACE", "").strip() or "default",
            "d1_helper": str(helper_pub),
            "d1_feedback_helper": str(helper_fb),
            "d1_helper_ok": helper_pub.exists(),
            "d1_feedback_helper_ok": helper_fb.exists(),
            "d1_binaries_ok": d1_binaries_ok,
            "modules": modules,
        }
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def object_detector_stack_status() -> dict[str, Any]:
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from box_object_detector import detector_status

        return detector_status()
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def nx_stack_status() -> dict[str, Any]:
    bind = os.environ.get("GO2_DASHBOARD_BIND", "0.0.0.0")
    return {
        "go2_local": go2_local(),
        "dashboard_bind": bind,
        "pid": os.getpid(),
        "hostname": platform.node(),
        "cameras": CAMERA_CACHE.stats() if go2_local() else {},
        "command_stack": command_stack_status(),
        "object_detector": object_detector_stack_status(),
        "real_arm_env": os.environ.get("GO2_ENABLE_REAL_ARM", "0"),
        "base_motion_env": os.environ.get("GO2_ENABLE_BASE_MOTION", "0"),
    }

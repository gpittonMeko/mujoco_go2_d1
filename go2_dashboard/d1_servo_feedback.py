"""Lettura feedback servo D1 — implementazione in D1 550 Workspace/OLD."""

from __future__ import annotations

import importlib.util
from typing import Any

from go2_dashboard.paths import D1_SERVO_FEEDBACK_PY

_spec = importlib.util.spec_from_file_location(
    "go2_dashboard._d1_servo_feedback_impl",
    D1_SERVO_FEEDBACK_PY,
)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load D1 servo feedback from {D1_SERVO_FEEDBACK_PY}")
_impl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_impl)

read_servo_deg_with_diag = _impl.read_servo_deg_with_diag

__all__ = ["read_servo_deg_with_diag"]

"""Payload viewer 3D leggero (catena FK braccio, frame arm_base)."""

from __future__ import annotations

import math
from typing import Any

from go2_dashboard.paths import ensure_d1_scripts_on_sys_path

ensure_d1_scripts_on_sys_path()

import arm_kinematics_d1_template as kin  # noqa: E402


def build_arm_viz_payload(servo_deg: list[float]) -> dict[str, Any]:
    q = [math.radians(float(servo_deg[i])) for i in range(6)]
    chain = kin.fk_chain_positions(q)
    tip = kin.fk_tool_tip(kin._clamp_q(q))
    return {
        "ok": True,
        "frame": "arm_base",
        "chain_arm_m": chain,
        "tool_tip_arm_m": [round(float(tip[i]), 5) for i in range(3)],
        "servo_deg": [round(float(servo_deg[i]), 3) for i in range(min(7, len(servo_deg)))],
        "pose_is_feedback": True,
        "axes_note": "+X avanti · +Y sinistra · +Z su (viewer: Y Three ≈ Z robot)",
    }

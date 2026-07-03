"""
Movimenti TCP eseguiti interamente sulla NX (nessun round-trip HTTP per tick).

Interpolazione lineare nello spazio cartesiano → IK → burst DDS via daemon.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any

from go2_dashboard.d1_jog import cartesian, motion_profile, service
from go2_dashboard.d1_jog.motion_guard import safety_preempt_active


def _segment_mm() -> float:
    return max(1.0, float(os.environ.get("D1_TCP_MOVE_SEGMENT_MM", "4")))


def _stream_hz() -> float:
    return max(20.0, min(60.0, float(os.environ.get("D1_TCP_MOVE_HZ", "50"))))


def _axis_delta_mm(axis: str, sign: float, delta_mm: float) -> tuple[float, float, float]:
    ax = (axis or "x").strip().lower()
    sgn = 1.0 if float(sign) >= 0 else -1.0
    d = float(delta_mm) * sgn / 1000.0
    if ax == "x":
        return d, 0.0, 0.0
    if ax == "y":
        return 0.0, d, 0.0
    return 0.0, 0.0, d


def plan_tcp_linear_waypoints(
    servo_deg: list[float], *, axis: str, sign: float, delta_mm: float
) -> tuple[list[list[float]] | None, dict[str, Any]]:
    """Pianifica waypoints giunto per spostamento lineare TCP (frame base braccio)."""
    abs_mm = abs(float(delta_mm))
    if abs_mm < 0.01:
        return None, {"ok": False, "reason": "zero_delta_mm"}
    dx, dy, dz = _axis_delta_mm(axis, sign, delta_mm)
    seg = _segment_mm()
    steps = max(2, min(80, int(math.ceil(abs_mm / seg))))
    waypoints, err = cartesian._interpolated_servo_waypoints(servo_deg, dx, dy, dz, steps=steps)
    meta = {
        "steps": steps,
        "segment_mm": round(abs_mm / steps, 2),
        "delta_mm": round(float(delta_mm) * (1.0 if sign >= 0 else -1.0), 2),
        "axis": axis,
    }
    if waypoints is None:
        return None, {"ok": False, "reason": err or "ik_failed", **meta}
    return waypoints, {"ok": True, **meta}


def execute_waypoints_local(waypoints: list[list[float]]) -> dict[str, Any]:
    """Invia waypoints sul daemon DDS locale — un messaggio per ciclo, senza HTTP."""
    if not waypoints:
        return {"ok": True, "count": 0}
    if safety_preempt_active():
        return {"ok": False, "reason": "motion_preempted:safety", "sent": 0}
    delay_ms = motion_profile.stream_delay_ms()
    hz = motion_profile.stream_hz()
    dt_s = 1.0 / hz
    service.ensure_command_daemon(delay_ms)
    if not service._arm_coupled:
        return {"ok": False, "reason": "not_coupled", "hint": "Premi Coppia ON"}
    sent = 0
    t0 = time.perf_counter()
    for i, sd in enumerate(waypoints):
        if safety_preempt_active():
            return {"ok": False, "reason": "motion_preempted:safety", "sent": sent}
        seq = (int(time.time()) % 100000) + i
        pub = service.publish_messages_stream(
            [service._pose_message(sd, seq=seq)],
            delay_ms=delay_ms,
        )
        if not (pub.get("ok") or pub.get("skipped")):
            return {"ok": False, "reason": pub.get("reason", "publish_failed"), "sent": sent}
        sent += 1
        elapsed = time.perf_counter() - t0
        sleep_s = max(0.0, (i + 1) * dt_s - elapsed)
        if sleep_s > 0:
            time.sleep(sleep_s)
    return {"ok": True, "sent": sent, "hz": hz, "delay_ms": delay_ms}


def move_tcp_axis_local(
    *,
    axis: str,
    sign: float,
    delta_mm: float,
    servo_deg: list[float] | None = None,
) -> dict[str, Any]:
    """Movimento lineare TCP locale (es. +100 mm su X) — tutto sulla NX."""
    if safety_preempt_active():
        return {"ok": False, "reason": "motion_preempted:safety"}
    if servo_deg is None:
        fb = service.read_servo_deg(fast=True)
        if not fb.get("ok") or not fb.get("servo_deg"):
            return {"ok": False, "reason": fb.get("reason", "no_feedback")}
        sd = fb["servo_deg"]
    else:
        sd = service.clamp_servo_deg(servo_deg)
    pose_before = cartesian.tcp_pose_m(sd)
    waypoints, plan = plan_tcp_linear_waypoints(sd, axis=axis, sign=sign, delta_mm=delta_mm)
    if waypoints is None:
        return {**plan, "pose_before": pose_before}
    exec_out = execute_waypoints_local(waypoints)
    target_sd = waypoints[-1]
    pose_after = cartesian.tcp_pose_m(target_sd)
    return {
        **exec_out,
        **plan,
        "pose_before": pose_before,
        "pose_after": pose_after,
        "target_servo_deg": target_sd,
        "waypoints": len(waypoints),
        "coupling_maintained": True,
        "action": "move_tcp_local",
    }

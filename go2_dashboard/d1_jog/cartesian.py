"""Jog Cartesiano TCP nel frame base braccio (FK/IK da ``arm_kinematics_d1_template``)."""

from __future__ import annotations

import math
import os
import time
from typing import Any

import numpy as np

from go2_dashboard.d1_jog import service
from go2_dashboard.paths import ensure_d1_scripts_on_sys_path

ensure_d1_scripts_on_sys_path()

import arm_kinematics_d1_template as kin  # noqa: E402


def _gripper_hold(servo_deg: list[float]) -> float:
    return float(servo_deg[6]) if len(servo_deg) > 6 else 50.0


def servo_deg_to_q_rad(servo_deg: list[float]) -> np.ndarray:
    return np.deg2rad([float(servo_deg[i]) for i in range(6)])


def q_rad_to_servo_deg(q: np.ndarray | list, gripper: float) -> list[float]:
    qn = np.asarray(q, dtype=float).reshape(6)
    out = [round(math.degrees(float(qn[i])), 3) for i in range(6)]
    out.append(round(float(gripper), 3))
    return service.clamp_servo_deg(out)


def tcp_pose_m(servo_deg: list[float]) -> dict[str, Any]:
    """Posizione punta utensile nel frame base braccio (arm_link00 / FK)."""
    q = servo_deg_to_q_rad(servo_deg)
    tip = kin.fk_tool_tip(q)
    return {
        "xyz_m": [round(float(tip[i]), 5) for i in range(3)],
        "frame": "arm_base",
        "axes_note": "+X avanti, +Y sinistra, +Z su (riferimento FK D1)",
    }


def _clamp_target_xyz(x: float, y: float, z: float) -> tuple[float, float, float]:
    if os.environ.get("D1_CART_SKIP_WORKSPACE_CLAMP", "").lower() in {"1", "true", "yes"}:
        return x, y, z
    return kin._clamp_workspace(x, y, z)


def _ik_to_servo_deg(
    x: float, y: float, z: float, *, seed_rad: np.ndarray | None, gripper: float
) -> list[float] | None:
    seed = None if seed_rad is None else seed_rad.tolist()
    q = kin.ik_reach(x, y, z, primary_seed=seed)
    if q is None:
        return None
    return q_rad_to_servo_deg(q, gripper)


def _differential_step(servo_deg: list[float], dx: float, dy: float, dz: float) -> list[float] | None:
    """Piccolo passo con Jacobiano numerico (veloce per jog continuo)."""
    q = kin._clamp_q(servo_deg_to_q_rad(servo_deg))
    tip = kin.fk_tool_tip(q)
    J = kin._numeric_jacobian(q, tip)
    delta = np.array([dx, dy, dz], dtype=float)
    lam = float(os.environ.get("D1_CART_DLS_LAMBDA", "0.002"))
    try:
        dq = J.T @ np.linalg.solve(J @ J.T + lam * np.eye(3), delta)
    except np.linalg.LinAlgError:
        return None
    from go2_dashboard.d1_jog import motion_profile

    max_dq = motion_profile.max_dq_rad()
    n = float(np.linalg.norm(dq))
    if n > max_dq and n > 1e-9:
        dq *= max_dq / n
    q_new = kin._clamp_q(q + dq)
    return q_rad_to_servo_deg(q_new, _gripper_hold(servo_deg))


def _interpolated_servo_waypoints(
    servo_deg: list[float],
    dx: float,
    dy: float,
    dz: float,
    *,
    steps: int,
) -> tuple[list[list[float]] | None, str | None]:
    """Waypoints giunto: interpolazione lineare TCP → IK per ogni frazione."""
    grip = _gripper_hold(servo_deg)
    q0 = servo_deg_to_q_rad(servo_deg)
    tip0 = kin.fk_tool_tip(q0)
    target = tip0 + np.array([dx, dy, dz], dtype=float)
    tx, ty, tz = _clamp_target_xyz(float(target[0]), float(target[1]), float(target[2]))
    target = np.array([tx, ty, tz], dtype=float)
    delta = target - tip0

    waypoints: list[list[float]] = []
    seed = q0
    n = max(1, int(steps))
    for i in range(1, n + 1):
        alpha = float(i) / float(n)
        t = tip0 + alpha * delta
        sd = _ik_to_servo_deg(float(t[0]), float(t[1]), float(t[2]), seed_rad=seed, gripper=grip)
        if sd is None:
            return None, f"ik_failed_step_{i}_of_{n}"
        waypoints.append(sd)
        seed = servo_deg_to_q_rad(sd)
    return waypoints, None


def _interpolation_plan(step_mm: float) -> tuple[int, int]:
    """
    Waypoint e delay in base alla distanza (mm).
    Meno messaggi e delay più bassi → jog cartesiano più reattivo.
    """
    abs_mm = abs(float(step_mm))
    segment = float(os.environ.get("D1_CART_SEGMENT_MM", "8"))
    max_wp = max(2, int(os.environ.get("D1_CART_MAX_WAYPOINTS", "6")))
    min_wp = max(1, int(os.environ.get("D1_CART_MIN_WAYPOINTS", "2")))
    n = int(math.ceil(abs_mm / max(segment, 1.0))) if abs_mm > 0.5 else min_wp
    n = max(min_wp, min(max_wp, n))
    delay_ms = max(6, int(os.environ.get("D1_CART_STEP_DELAY_MS", "10")))
    return n, delay_ms


def _waypoints_to_messages(waypoints: list[list[float]]) -> list[dict[str, Any]]:
    seq_base = int(time.time()) % 100000
    mode = int(os.environ.get("D1_JOG_MODE", "1"))
    msgs: list[dict[str, Any]] = []
    for i, sd in enumerate(waypoints):
        data: dict[str, Any] = {"mode": mode}
        for j, ang in enumerate(sd[:7]):
            data[f"angle{j}"] = ang
        msgs.append({"seq": seq_base + i, "address": 1, "funcode": 2, "data": data})
    return msgs


def _publish_waypoint_trajectory(waypoints: list[list[float]], *, delay_ms: int) -> dict[str, Any]:
    """Traiettoria interpolata sul daemon ``d1_sdk_command`` (stesso percorso di grasp/lite)."""
    return service.publish_trajectory_stream(_waypoints_to_messages(waypoints), delay_ms=delay_ms)


def _velocity_mm_s(velocity_pct: float, max_speed_mm_s: float | None = None) -> float:
    from go2_dashboard.d1_jog import motion_profile

    pct = max(1.0, min(100.0, float(velocity_pct)))
    vmax = float(max_speed_mm_s) if max_speed_mm_s and max_speed_mm_s > 0 else motion_profile.max_speed_mm_s()
    vmin = motion_profile.min_speed_mm_s()
    return vmin + (vmax - vmin) * (pct / 100.0)


def cartesian_jog_tick(
    servo_deg: list[float],
    *,
    axis: str,
    sign: float = 1.0,
    velocity_pct: float = 25.0,
    dt_s: float = 0.05,
    max_speed_mm_s: float | None = None,
) -> dict[str, Any]:
    """
    Un tick di jog continuo (stile teach pendant UR): piccolo spostamento TCP
    proporzionale a velocità × dt, un solo comando DDS (Jacobiano).
    """
    ax = (axis or "x").strip().lower()
    if ax not in {"x", "y", "z"}:
        return {"ok": False, "reason": "axis must be x, y, or z"}
    sgn = 1.0 if float(sign) >= 0 else -1.0
    dt = max(0.008, min(0.12, float(dt_s)))
    speed_m_s = _velocity_mm_s(velocity_pct, max_speed_mm_s) / 1000.0
    dist_m = speed_m_s * dt * sgn
    cap = float(os.environ.get("D1_JOG_TICK_MAX_MM", "12")) / 1000.0
    if abs(dist_m) > cap:
        dist_m = cap if dist_m > 0 else -cap
    dx = dy = dz = 0.0
    if ax == "x":
        dx = dist_m
    elif ax == "y":
        dy = dist_m
    else:
        dz = dist_m

    pose_before = tcp_pose_m(servo_deg)
    target_sd = _differential_step(servo_deg, dx, dy, dz)
    if target_sd is None:
        target_sd = _ik_to_servo_deg(
            pose_before["xyz_m"][0] + dx,
            pose_before["xyz_m"][1] + dy,
            pose_before["xyz_m"][2] + dz,
            seed_rad=servo_deg_to_q_rad(servo_deg),
            gripper=_gripper_hold(servo_deg),
        )
    if target_sd is None:
        return {"ok": False, "reason": "ik_failed", "pose_before": pose_before}

    delay_ms = max(4, int(os.environ.get("D1_JOG_TICK_DELAY_MS", "6")))
    out = service.jog_pose_deg(target_sd)
    out["delay_ms"] = delay_ms
    out["continuous"] = True
    out["speed_mm_s"] = round(abs(dist_m) / dt * 1000.0, 2)
    out["dt_s"] = round(dt, 4)
    out["pose_before"] = pose_before
    out["pose_after"] = tcp_pose_m(target_sd)
    out["delta_mm"] = [round(dx * 1000.0, 3), round(dy * 1000.0, 3), round(dz * 1000.0, 3)]
    out["target_servo_deg"] = target_sd
    return out


def cartesian_nudge(
    servo_deg: list[float],
    *,
    axis: str,
    sign: float = 1.0,
    step_mm: float | None = None,
    interpolated: bool | None = None,
) -> dict[str, Any]:
    """
    Sposta il TCP lungo un asse del frame base.

    axis: x | y | z
    sign: +1 o -1
    """
    ax = (axis or "x").strip().lower()
    if ax not in {"x", "y", "z"}:
        return {"ok": False, "reason": "axis must be x, y, or z"}
    sgn = 1.0 if float(sign) >= 0 else -1.0
    step_m = float(step_mm if step_mm is not None else os.environ.get("D1_CART_STEP_MM", "5")) / 1000.0
    step_m *= sgn
    dx = dy = dz = 0.0
    if ax == "x":
        dx = step_m
    elif ax == "y":
        dy = step_m
    else:
        dz = step_m

    use_interp = interpolated
    if use_interp is None:
        use_interp = os.environ.get("D1_CART_INTERPOLATED", "1").lower() in {"1", "true", "yes"}

    pose_before = tcp_pose_m(servo_deg)

    if use_interp:
        steps, delay_ms = _interpolation_plan(step_m * 1000.0)
        waypoints, err = _interpolated_servo_waypoints(servo_deg, dx, dy, dz, steps=steps)
        if waypoints is None:
            return {
                "ok": False,
                "reason": err or "ik_failed",
                "pose_before": pose_before,
            }
        out = _publish_waypoint_trajectory(waypoints, delay_ms=delay_ms)
        out["delay_ms"] = delay_ms
        out["waypoints"] = len(waypoints)
        out["interpolated"] = True
        target_sd = waypoints[-1]
    else:
        target_sd = _differential_step(servo_deg, dx, dy, dz)
        if target_sd is None:
            target_sd = _ik_to_servo_deg(
                pose_before["xyz_m"][0] + dx,
                pose_before["xyz_m"][1] + dy,
                pose_before["xyz_m"][2] + dz,
                seed_rad=servo_deg_to_q_rad(servo_deg),
                gripper=_gripper_hold(servo_deg),
            )
        if target_sd is None:
            return {"ok": False, "reason": "ik_failed", "pose_before": pose_before}
        out = service.jog_pose_deg(target_sd)
        out["waypoints"] = 1
        out["interpolated"] = False

    pose_after = tcp_pose_m(target_sd)
    out["pose_before"] = pose_before
    out["pose_after"] = pose_after
    out["delta_mm"] = [round(dx * 1000.0, 2), round(dy * 1000.0, 2), round(dz * 1000.0, 2)]
    out["target_servo_deg"] = target_sd
    return out


def cartesian_move_delta(
    servo_deg: list[float],
    *,
    dx_m: float = 0.0,
    dy_m: float = 0.0,
    dz_m: float = 0.0,
    interpolated: bool = True,
) -> dict[str, Any]:
    """Spostamento TCP arbitrario (metri) nel frame base."""
    norm = math.sqrt(dx_m * dx_m + dy_m * dy_m + dz_m * dz_m)
    if norm < 1e-9:
        return {"ok": False, "reason": "zero_delta"}
    if interpolated:
        step_mm = math.sqrt(dx_m * dx_m + dy_m * dy_m + dz_m * dz_m) * 1000.0
        steps, delay_ms = _interpolation_plan(step_mm)
        waypoints, err = _interpolated_servo_waypoints(servo_deg, dx_m, dy_m, dz_m, steps=steps)
        if waypoints is None:
            return {"ok": False, "reason": err or "ik_failed"}
        out = _publish_waypoint_trajectory(waypoints, delay_ms=delay_ms)
        out["delay_ms"] = delay_ms
        out["target_servo_deg"] = waypoints[-1]
        out["waypoints"] = len(waypoints)
    else:
        tip = tcp_pose_m(servo_deg)["xyz_m"]
        target_sd = _ik_to_servo_deg(
            tip[0] + dx_m,
            tip[1] + dy_m,
            tip[2] + dz_m,
            seed_rad=servo_deg_to_q_rad(servo_deg),
            gripper=_gripper_hold(servo_deg),
        )
        if target_sd is None:
            return {"ok": False, "reason": "ik_failed"}
        out = service.jog_pose_deg(target_sd)
        out["target_servo_deg"] = target_sd
        out["waypoints"] = 1
    out["pose_before"] = tcp_pose_m(servo_deg)
    out["pose_after"] = tcp_pose_m(out["target_servo_deg"])
    return out

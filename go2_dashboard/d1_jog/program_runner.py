"""Esecuzione programmi a punti — movimento fluido mode 1 (stile zero)."""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from go2_dashboard.d1_jog import cartesian, motion_profile, program_store, service
from go2_dashboard.d1_jog.motion_guard import release as motion_release, try_acquire as motion_try_acquire

_lock = threading.Lock()
_running = False
_stop_requested = False
_status: dict[str, Any] = {"running": False, "index": -1, "total": 0, "waypoint_id": None, "error": None}


def execution_status() -> dict[str, Any]:
    with _lock:
        return dict(_status)


def request_stop() -> dict[str, Any]:
    global _stop_requested
    _stop_requested = True
    service._halt_cartesian_stream(wait_idle=True)
    return {"ok": True, "action": "program_stop_requested"}


def _max_joint_step_deg() -> float:
    return max(0.5, float(os.environ.get("D1_PROG_JOINT_STEP_DEG", "2.0")))


def _settle_s() -> float:
    return max(0.1, float(os.environ.get("D1_PROG_SETTLE_S", "0.35")))


def _arm_tolerance_deg() -> float:
    """Range ±° sui giunti braccio (J0–J5) — non richiede uguaglianza esatta col salvato."""
    return max(1.0, float(os.environ.get("D1_PROG_POSITION_TOL_DEG", "2.5")))


def _gripper_tolerance_deg() -> float:
    return max(_arm_tolerance_deg(), float(os.environ.get("D1_PROG_GRIPPER_TOL_DEG", "8")))


def _soft_tolerance_deg() -> float:
    """Se il timeout scatta ma siamo ancora «abbastanza vicini», si prosegue (evita blocco)."""
    return max(_arm_tolerance_deg(), float(os.environ.get("D1_PROG_SOFT_TOL_DEG", "4.5")))


def _wait_timeout_s() -> float:
    return max(3.0, float(os.environ.get("D1_PROG_WAIT_TIMEOUT_S", "30")))


def _proceed_on_timeout() -> bool:
    return os.environ.get("D1_PROG_PROCEED_ON_TIMEOUT", "1").lower() in {"1", "true", "yes", "on"}


def _ignore_gripper_check() -> bool:
    return os.environ.get("D1_PROG_IGNORE_GRIPPER", "1").lower() in {"1", "true", "yes", "on"}


def _move_deg_per_s() -> float:
    return max(3.0, float(os.environ.get("D1_PROG_MOVE_DEG_PER_S", "12")))


def _joint_errors(current: list[float], target: list[float]) -> list[float]:
    n = min(7, len(current), len(target))
    return [round(abs(float(current[i]) - float(target[i])), 2) for i in range(n)]


def _within_target_range(current: list[float], target: list[float]) -> tuple[bool, float, list[float]]:
    """True se ogni giunto rilevante è dentro il range ±tol rispetto al punto salvato."""
    cur = service.clamp_servo_deg(current)
    tgt = service.clamp_servo_deg(target)
    errs = _joint_errors(cur, tgt)
    arm_tol = _arm_tolerance_deg()
    arm_ok = all(errs[i] <= arm_tol for i in range(min(6, len(errs))))
    if _ignore_gripper_check():
        ok = arm_ok
        max_err = max(errs[:6]) if errs else 0.0
    else:
        grip_tol = _gripper_tolerance_deg()
        ok = arm_ok and (len(errs) < 7 or errs[6] <= grip_tol)
        max_err = max(errs) if errs else 0.0
    return ok, max_err, errs


def _estimate_move_duration_s(from_sd: list[float], to_sd: list[float]) -> float:
    max_d = max(abs(float(to_sd[i]) - float(from_sd[i])) for i in range(7))
    return max(_settle_s(), max_d / _move_deg_per_s())


def wait_until_at_target(
    target_servo_deg: list[float],
    *,
    stop_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Attende che i giunti siano nel range ±tol intorno al punto salvato (non uguaglianza esatta)."""
    target = service.clamp_servo_deg(target_servo_deg)
    arm_tol = _arm_tolerance_deg()
    soft_tol = _soft_tolerance_deg()
    deadline = time.monotonic() + _wait_timeout_s()
    last_cur: list[float] | None = None
    last_max_err: float | None = None
    last_errs: list[float] | None = None
    polls = 0
    max_polls = max(3, int(os.environ.get("D1_PROG_MAX_POLLS", "12")))

    while time.monotonic() < deadline and polls < max_polls:
        if stop_check and stop_check():
            return {
                "ok": False,
                "reason": "stopped",
                "action": "wait_at_target",
                "target_servo_deg": target,
                "last_servo_deg": last_cur,
                "max_error_deg": last_max_err,
            }
        try:
            from go2_dashboard import d1_arm_motion

            if d1_arm_motion.is_live_session_active():
                service.jog_pose_deg(target, keep_lock=True)
            else:
                service.hold_pose_stream(servo_deg=target)
        except Exception:
            service.hold_pose_stream(servo_deg=target)
        fb = service.read_servo_deg(fast=True)
        polls += 1
        if fb.get("ok") and fb.get("servo_deg"):
            cur = fb["servo_deg"]
            last_cur = cur
            ok, max_err, errs = _within_target_range(cur, target)
            last_max_err = round(max_err, 2)
            last_errs = errs
            if ok:
                service.set_servo_cache(service.clamp_servo_deg(cur))
                service.hold_pose_stream(servo_deg=target)
                return {
                    "ok": True,
                    "action": "wait_at_target",
                    "reached": True,
                    "target_servo_deg": target,
                    "servo_deg": service.clamp_servo_deg(cur),
                    "max_error_deg": last_max_err,
                    "tolerance_deg": arm_tol,
                    "joint_errors_deg": errs,
                    "polls": polls,
                }
        time.sleep(max(0.05, float(os.environ.get("D1_PROG_POLL_GAP_S", "0.15"))))

    if _proceed_on_timeout() and last_max_err is not None and last_max_err <= soft_tol:
        service.set_servo_cache(target)
        service.hold_pose_stream(servo_deg=target)
        return {
            "ok": True,
            "action": "wait_at_target",
            "reached": True,
            "reached_approx": True,
            "target_servo_deg": target,
            "servo_deg": last_cur or target,
            "max_error_deg": last_max_err,
            "tolerance_deg": arm_tol,
            "soft_tolerance_deg": soft_tol,
            "joint_errors_deg": last_errs,
            "polls": polls,
            "note": "within_soft_range_after_wait",
        }

    return {
        "ok": False,
        "reason": "position_timeout",
        "action": "wait_at_target",
        "target_servo_deg": target,
        "last_servo_deg": last_cur,
        "max_error_deg": last_max_err,
        "tolerance_deg": arm_tol,
        "soft_tolerance_deg": soft_tol,
        "joint_errors_deg": last_errs,
        "polls": polls,
        "timeout_s": _wait_timeout_s(),
    }


def plan_joint_waypoints(
    from_sd: list[float], to_sd: list[float], *, max_step_deg: float | None = None
) -> list[list[float]]:
    step = max_step_deg if max_step_deg is not None else _max_joint_step_deg()
    diffs = [abs(float(to_sd[i]) - float(from_sd[i])) for i in range(7)]
    n = max(2, int(math.ceil(max(diffs + [0.01]) / step)))
    out: list[list[float]] = []
    for k in range(1, n + 1):
        alpha = float(k) / float(n)
        wp = [
            round(float(from_sd[i]) + alpha * (float(to_sd[i]) - float(from_sd[i])), 3)
            for i in range(7)
        ]
        out.append(service.clamp_servo_deg(wp))
    return out


def move_to_servo_deg_smooth(
    target_servo_deg: list[float],
    *,
    keep_lock: bool = False,
    stop_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Interpola in spazio giunti con funcode 2 mode 1."""
    if not keep_lock:
        ok, busy = motion_try_acquire("program")
        if not ok:
            return {"ok": False, "reason": busy, "action": "move_to_point"}
    try:
        service._halt_cartesian_stream(wait_idle=True)
        fb = service.read_servo_deg(fast=True)
        if not fb.get("ok") or not fb.get("servo_deg"):
            return {"ok": False, "reason": fb.get("reason", "no_feedback")}
        cur = fb["servo_deg"]
        target = service.clamp_servo_deg(target_servo_deg)
        waypoints = plan_joint_waypoints(cur, target)
        delay_ms = motion_profile.stream_delay_ms()
        if not service.ensure_command_daemon(delay_ms):
            return {"ok": False, "reason": "daemon_start_failed"}
        if not service._arm_coupled:
            return {
                "ok": False,
                "reason": "not_coupled",
                "hint": "Premi Coppia ON",
                "action": "move_to_point",
            }
        service.set_servo_cache(cur)
        sent = 0
        for i, sd in enumerate(waypoints):
            msg = service._pose_message(sd, seq=int(time.time()) % 100000 + i)
            pub = service.publish_messages_stream([msg], delay_ms=delay_ms)
            if not (pub.get("ok") or pub.get("skipped")):
                return {"ok": False, "reason": pub.get("reason", "publish_failed"), "sent": sent}
            sent += 1
            service.set_servo_cache(sd)
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)
        time.sleep(_estimate_move_duration_s(cur, target))
        service.hold_pose_stream(servo_deg=target)
        wait = wait_until_at_target(target, stop_check=stop_check)
        if not wait.get("ok"):
            return {
                "ok": False,
                "reason": wait.get("reason", "wait_failed"),
                "action": "move_to_point",
                "waypoints": sent,
                "target_servo_deg": target,
                "wait_at_target": wait,
            }
        hold = service.hold_pose_stream(servo_deg=target)
        pose_sd = wait.get("servo_deg") or target
        return {
            "ok": True,
            "action": "move_to_point",
            "waypoints": len(waypoints),
            "target_servo_deg": target,
            "pose_after": cartesian.tcp_pose_m(pose_sd),
            "coupling_maintained": bool(hold.get("ok") or hold.get("skipped")),
            "wait_at_target": wait,
            "max_error_deg": wait.get("max_error_deg"),
        }
    finally:
        if not keep_lock:
            motion_release("program")


def _run_program_thread(program_id: str) -> None:
    global _running, _stop_requested, _status
    prog = program_store.load_program(program_id)
    if prog is None:
        with _lock:
            _status = {"running": False, "error": "program_not_found", "index": -1, "total": 0}
        _running = False
        return
    wps = list(prog.get("waypoints") or [])
    with _lock:
        _status = {
            "running": True,
            "program_id": program_id,
            "index": 0,
            "total": len(wps),
            "waypoint_id": None,
            "error": None,
        }
    for i, wp in enumerate(wps):
        if _stop_requested:
            break
        with _lock:
            _status["index"] = i
            _status["waypoint_id"] = wp.get("id")
            _status["waypoint_name"] = wp.get("name")
        sd = wp.get("servo_deg")
        if not isinstance(sd, list) or len(sd) < 6:
            with _lock:
                _status["error"] = f"invalid_waypoint_{wp.get('id')}"
            break
        out = move_to_servo_deg_smooth(sd, keep_lock=True, stop_check=lambda: _stop_requested)
        if not out.get("ok"):
            with _lock:
                _status["error"] = out.get("reason", "move_failed")
            break
    with _lock:
        _status["running"] = False
        _status["finished"] = not _stop_requested and _status.get("error") is None
    _running = False
    service.hold_pose_stream()


def run_program(program_id: str) -> dict[str, Any]:
    global _running, _stop_requested

    if _running:
        return {"ok": False, "reason": "program_already_running"}
    ok, busy = motion_try_acquire("program")
    if not ok:
        return {"ok": False, "reason": busy}
    _stop_requested = False
    _running = True

    def _wrapper() -> None:
        global _running
        try:
            _run_program_thread(program_id)
        finally:
            motion_release("program")
            _running = False

    threading.Thread(target=_wrapper, name="d1_program_run", daemon=True).start()
    return {"ok": True, "action": "program_run_started", "program_id": program_id}

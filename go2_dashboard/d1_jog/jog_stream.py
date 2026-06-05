"""
Jog cartesiano continuo lato server (thread dedicato sulla NX).

Passi TCP con Jacobiano (come teach pendant UR) — niente integratore TCP + IK globale.
Il thread NON invia comandi quando non è in movimento (evita comandi fantasma dopo release).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from go2_dashboard.d1_jog import cartesian, motion_profile, service

_lock = threading.Lock()
_thread: threading.Thread | None = None
_started = False

_state: dict[str, Any] = {
    "armed": False,
    "running": False,
    "axis": "x",
    "sign": 1.0,
    "velocity_pct": 30.0,
    "max_speed_mm_s": None,
    "accel_mm_s2": None,
    "decel_mm_s2": None,
    "target_speed_mm_s": 0.0,
    "current_speed_mm_s": 0.0,
    "servo_deg": None,
    "tcp_m": None,
    "last_error": None,
    "ticks": 0,
}
def is_motion_active() -> bool:
    with _lock:
        if not bool(_state["armed"]):
            return False
        running = bool(_state["running"])
        cur = float(_state["current_speed_mm_s"])
    return running or cur > 0.4


def _accel_mm_s2() -> float:
    return motion_profile.accel_mm_s2()


def _decel_mm_s2() -> float:
    return motion_profile.decel_mm_s2()


def _stream_hz() -> float:
    return motion_profile.stream_hz()


def _stream_delay_ms() -> int:
    return motion_profile.stream_delay_ms()


def _min_move_speed_mm_s() -> float:
    return motion_profile.min_move_speed_mm_s()


def _kick_start_ratio() -> float:
    return motion_profile.kick_start_ratio()


def _ramp_speed(current: float, target: float, dt_s: float, accel: float, decel: float) -> float:
    if target > current:
        return min(target, current + accel * dt_s)
    if target < current:
        return max(target, current - decel * dt_s)
    return current


def _stream_mode() -> int:
    return motion_profile.smooth_mode()


def _msgs_for_servo(servo_deg: list[float], seq: int) -> dict[str, Any]:
    data: dict[str, Any] = {"mode": _stream_mode()}
    for j, ang in enumerate(servo_deg[:7]):
        data[f"angle{j}"] = ang
    return {"seq": seq, "address": 1, "funcode": 2, "data": data}


def _send_servo_target(servo_deg: list[float]) -> dict[str, Any]:
    """Solo funcode 2 — coppia già data da cartesian_begin / Coppia ON."""
    with _lock:
        if not bool(_state["armed"]):
            return {"ok": True, "skipped": True, "reason": "stream_disarmed"}
    seq = int(time.time()) % 100000
    out = service.publish_messages_stream(
        [_msgs_for_servo(servo_deg, seq)],
        delay_ms=_stream_delay_ms(),
    )
    if out.get("ok") or out.get("skipped"):
        return out
    if not service.ensure_command_daemon(_stream_delay_ms()):
        return {"ok": False, "reason": "daemon_restart_failed"}
    return service.publish_messages_stream(msgs, delay_ms=_stream_delay_ms())


def _axis_delta_m(axis: str, sign: float, dist_m: float) -> tuple[float, float, float]:
    sgn = 1.0 if float(sign) >= 0 else -1.0
    d = float(dist_m) * sgn
    ax = (axis or "x").lower()
    if ax == "x":
        return d, 0.0, 0.0
    if ax == "y":
        return 0.0, d, 0.0
    return 0.0, 0.0, d


def halt_completely() -> None:
    """Ferma movimento e disarma il thread — nessun comando DDS successivo."""
    with _lock:
        _state["armed"] = False
        _state["running"] = False
        _state["target_speed_mm_s"] = 0.0
        _state["current_speed_mm_s"] = 0.0
        _state["servo_deg"] = None
        _state["tcp_m"] = None
        _state["last_error"] = None
        _state["ticks"] = 0


def _stream_loop() -> None:
    while True:
        t0 = time.perf_counter()
        with _lock:
            armed = bool(_state["armed"])
            running = bool(_state["running"])
            axis = str(_state["axis"])
            sign = float(_state["sign"])
            target = float(_state["target_speed_mm_s"]) if running else 0.0
            accel = float(_state["accel_mm_s2"] or _accel_mm_s2())
            decel = float(_state["decel_mm_s2"] or _decel_mm_s2())
            cur_speed = float(_state["current_speed_mm_s"])
            sd0 = _state["servo_deg"]
            if sd0 is not None:
                sd0 = list(sd0)

        if not armed or not sd0:
            time.sleep(0.02)
            continue

        if not running and abs(cur_speed) < 0.12:
            time.sleep(0.01)
            continue

        hz = _stream_hz()
        dt = 1.0 / hz
        speed_after = _ramp_speed(cur_speed, target, dt, accel, decel)
        avg_speed = 0.5 * (cur_speed + speed_after)
        floor = _min_move_speed_mm_s()
        if running and target > 0.5 and floor > 0:
            avg_speed = max(avg_speed, floor)

        dist_m = motion_profile.cap_step_mm(avg_speed / 1000.0 * dt)
        err: str | None = None
        sd = sd0
        tcp_m: list[float] | None = None

        if dist_m >= 1e-8:
            dx, dy, dz = _axis_delta_m(axis, sign, dist_m)
            sd_next = cartesian._differential_step(sd0, dx, dy, dz)
            if sd_next is None:
                err = "differential_ik_failed"
            else:
                sd = sd_next
                tcp_m = list(cartesian.tcp_pose_m(sd)["xyz_m"])

        if sd is not None and err is None and dist_m >= 1e-8:
            pub = _send_servo_target(sd)
            if not (pub.get("ok") or pub.get("skipped")):
                err = str(pub.get("reason", "publish_failed"))

        with _lock:
            if not bool(_state["armed"]):
                elapsed = time.perf_counter() - t0
                sleep_s = max(0.0, (1.0 / _stream_hz()) - elapsed)
                if sleep_s > 0:
                    time.sleep(sleep_s)
                continue
            _state["current_speed_mm_s"] = speed_after if err is None else 0.0
            if sd is not None and tcp_m is not None and err is None:
                _state["servo_deg"] = sd
                _state["tcp_m"] = tcp_m
            _state["last_error"] = err
            _state["ticks"] = int(_state.get("ticks", 0)) + 1
            if err:
                _state["running"] = False
                _state["target_speed_mm_s"] = 0.0

        elapsed = time.perf_counter() - t0
        sleep_s = max(0.0, dt - elapsed)
        if sleep_s > 0:
            time.sleep(sleep_s)


def _ensure_thread() -> None:
    global _thread, _started
    if _started:
        return
    _thread = threading.Thread(target=_stream_loop, name="d1_jog_stream", daemon=True)
    _thread.start()
    _started = True


def jog_start(
    *,
    axis: str,
    sign: float,
    velocity_pct: float = 30.0,
    max_speed_mm_s: float | None = None,
    accel_mm_s2: float | None = None,
    decel_mm_s2: float | None = None,
    servo_deg: list[float] | None = None,
) -> dict[str, Any]:
    if servo_deg is None:
        cached = service.get_servo_cache()
        if cached is not None:
            sd = cached
        else:
            fb = service.read_servo_deg(fast=True)
            if not fb.get("ok") or not fb.get("servo_deg"):
                return {"ok": False, "reason": fb.get("reason", "no_feedback")}
            sd = fb["servo_deg"]
    else:
        sd = service.clamp_servo_deg(servo_deg)

    pose = cartesian.tcp_pose_m(sd)
    tcp_m = list(pose["xyz_m"])
    target = cartesian._velocity_mm_s(velocity_pct, max_speed_mm_s)
    kick = target * _kick_start_ratio()

    _ensure_thread()
    service.ensure_command_daemon(_stream_delay_ms())

    with _lock:
        _state["armed"] = True
        _state["running"] = True
        _state["axis"] = (axis or "x").strip().lower()
        _state["sign"] = 1.0 if float(sign) >= 0 else -1.0
        _state["velocity_pct"] = float(velocity_pct)
        _state["max_speed_mm_s"] = max_speed_mm_s
        _state["accel_mm_s2"] = accel_mm_s2
        _state["decel_mm_s2"] = decel_mm_s2
        _state["target_speed_mm_s"] = target
        _state["current_speed_mm_s"] = kick
        _state["servo_deg"] = sd
        _state["tcp_m"] = tcp_m
        _state["last_error"] = None
        _state["ticks"] = 0

    return {
        "ok": True,
        "streaming": True,
        "target_speed_mm_s": round(target, 2),
        "current_speed_mm_s": round(kick, 2),
        "axis": _state["axis"],
        "sign": _state["sign"],
        "stream_hz": _stream_hz(),
        "tcp_m": [round(x, 4) for x in tcp_m],
        "mode": "differential_cartesian",
        "smooth_mode": _stream_mode(),
    }


def jog_update(
    *,
    velocity_pct: float | None = None,
    max_speed_mm_s: float | None = None,
    accel_mm_s2: float | None = None,
    decel_mm_s2: float | None = None,
) -> dict[str, Any]:
    with _lock:
        if not _state["armed"] or not _state["running"]:
            return {"ok": False, "reason": "not_running"}
        pct = float(velocity_pct) if velocity_pct is not None else float(_state["velocity_pct"])
        max_sp = max_speed_mm_s if max_speed_mm_s is not None else _state["max_speed_mm_s"]
        if velocity_pct is not None:
            _state["velocity_pct"] = pct
        if max_speed_mm_s is not None:
            _state["max_speed_mm_s"] = max_sp
        if accel_mm_s2 is not None:
            _state["accel_mm_s2"] = accel_mm_s2
        if decel_mm_s2 is not None:
            _state["decel_mm_s2"] = decel_mm_s2
        _state["target_speed_mm_s"] = cartesian._velocity_mm_s(pct, max_sp)

    return {
        "ok": True,
        "target_speed_mm_s": round(_state["target_speed_mm_s"], 2),
        "current_speed_mm_s": round(float(_state["current_speed_mm_s"]), 2),
    }


def jog_stop() -> dict[str, Any]:
    with _lock:
        _state["running"] = False
        _state["target_speed_mm_s"] = 0.0
        cur = float(_state["current_speed_mm_s"])
    return {"ok": True, "streaming": False, "decelerating": True, "current_speed_mm_s": round(cur, 2)}


def jog_status() -> dict[str, Any]:
    with _lock:
        sd = _state["servo_deg"]
        tcp_m = _state["tcp_m"]
        armed = bool(_state["armed"])
        running = _state["running"]
        cur = float(_state["current_speed_mm_s"])
        tgt = float(_state["target_speed_mm_s"])
        err = _state["last_error"]
    out: dict[str, Any] = {
        "ok": True,
        "armed": armed,
        "running": running,
        "current_speed_mm_s": round(cur, 2),
        "target_speed_mm_s": round(tgt, 2),
        "last_error": err,
        "mode": "differential_cartesian",
        "smooth_mode": _stream_mode(),
    }
    if sd:
        out["servo_deg"] = sd
        out["pose"] = cartesian.tcp_pose_m(sd)
    if tcp_m:
        out["tcp_m"] = [round(float(x), 4) for x in tcp_m]
    return out

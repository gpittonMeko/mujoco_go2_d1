"""Profilo movimento «tipo zero» — funcode 2 mode 1 + passi piccoli e ritmo basso."""

from __future__ import annotations

import os


def smooth_mode() -> int:
    """mode 1 = smoothing traiettoria (doc Unitree); come movimento zero interno."""
    return int(os.environ.get("D1_JOG_STREAM_MODE", os.environ.get("D1_JOG_MODE", "1")))


def stream_hz() -> float:
    return max(8.0, min(25.0, float(os.environ.get("D1_JOG_STREAM_HZ", "12"))))


def stream_delay_ms() -> int:
    """Pausa tra waypoint programma (non il daemon jog live)."""
    return max(0, int(os.environ.get("D1_JOG_STREAM_DELAY_MS", "8")))


def daemon_delay_ms() -> int:
    """Sleep C++ dopo ogni riga stdin — 0 = minima latenza (rate limit solo lato Python)."""
    return max(0, int(os.environ.get("D1_JOG_DAEMON_DELAY_MS", "0")))


def joint_cmd_delay_ms() -> int:
    return daemon_delay_ms()


def max_speed_mm_s() -> float:
    return max(2.0, float(os.environ.get("D1_JOG_MAX_SPEED_MM_S", "18")))


def min_speed_mm_s() -> float:
    return max(0.0, float(os.environ.get("D1_JOG_MIN_SPEED_MM_S", "0")))


def accel_mm_s2() -> float:
    return max(20.0, float(os.environ.get("D1_JOG_ACCEL_MM_S2", "120")))


def decel_mm_s2() -> float:
    return max(20.0, float(os.environ.get("D1_JOG_DECEL_MM_S2", "150")))


def kick_start_ratio() -> float:
    return max(0.0, min(0.2, float(os.environ.get("D1_JOG_KICK_START_RATIO", "0"))))


def min_move_speed_mm_s() -> float:
    return max(0.0, float(os.environ.get("D1_JOG_MIN_MOVE_SPEED_MM_S", "0")))


def tick_max_mm() -> float:
    return max(0.2, float(os.environ.get("D1_JOG_TICK_MAX_MM", "2.5")))


def max_dq_rad() -> float:
    return max(0.005, float(os.environ.get("D1_CART_MAX_DQ_RAD", "0.035")))


def cap_step_mm(dist_m: float) -> float:
    cap_m = tick_max_mm() / 1000.0
    if abs(dist_m) <= cap_m:
        return dist_m
    return cap_m if dist_m > 0 else -cap_m

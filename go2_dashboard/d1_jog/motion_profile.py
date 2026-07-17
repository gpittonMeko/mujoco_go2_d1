"""Profilo movimento D1: mode 1 solo per traiettoria, mode 0 per hold statico.

Doc Unitree (D1 Arm services):
  mode 0 = small smoothing of 10Hz data
  mode 1 = large smoothing of trajectory use

Contratto hold (issue D1 / lab):
  - idle: heartbeat mode0 ~10Hz, un solo publisher
  - abort motion: soft hold = solo pose mode0 sulla MISURA (mai re-couple)
  - HOLD UI: hard = power + funcode 5 + pose (una volta)
  - vietato: hold measured poi snap al target software (= strattone)

Anti-pattern (Caltech SURF + lab): stream continuo mode1 a 20–100Hz fa
surriscaldare i servo e dopo 1–pochi minuti il braccio smette di rispondere
mentre il software crede ancora che HOLD sia attivo.
"""

from __future__ import annotations

import os


def smooth_mode() -> int:
    """mode 1 = smoothing traiettoria (doc Unitree); solo per waypoint/jog in moto."""
    return int(os.environ.get("D1_JOG_STREAM_MODE", os.environ.get("D1_JOG_MODE", "1")))


def hold_mode() -> int:
    """mode 0 = hold / keepalive a ~10Hz (doc Unitree). Non usare mode 1 per HOLD."""
    return int(os.environ.get("D1_HOLD_MODE", "0"))


def hold_heartbeat_ms() -> int:
    """Periodo heartbeat hold: default 100ms (=10Hz ciclo ufficiale D1)."""
    return max(80, min(500, int(os.environ.get("D1_HOLD_HEARTBEAT_MS", "100"))))


def stream_hz() -> float:
    # Cap soft a 12Hz: sopra il ciclo ufficiale 10Hz aumenta flood DDS/servo.
    return max(8.0, min(12.0, float(os.environ.get("D1_JOG_STREAM_HZ", "10"))))


def stream_delay_ms() -> int:
    """Pausa tra waypoint programma (non il daemon jog live)."""
    return max(0, int(os.environ.get("D1_JOG_STREAM_DELAY_MS", "8")))


def auto_move_mode() -> int:
    """AUTO calib: default mode0 (dati ~10Hz), non mode1 trajectory flood."""
    return int(os.environ.get("D1_GRASP6D_AUTO_MOVE_MODE", "0"))


def auto_waypoint_delay_ms() -> int:
    """Floor delay AUTO: <=10Hz comandi durante i piccoli offset."""
    return max(100, int(os.environ.get("D1_GRASP6D_AUTO_WAYPOINT_DELAY_MS", "120")))


def auto_joint_step_deg() -> float:
    """Passi giunto più grandi → meno waypoint → meno flood DDS."""
    return max(2.0, float(os.environ.get("D1_GRASP6D_AUTO_JOINT_STEP_DEG", "4.0")))


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

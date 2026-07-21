"""Telemetria motori Go2 da DDS ``rt/lowstate`` (temperatura, coppia, BMS, ventole)."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

try:
    from go2_dashboard.go2_thermal_protect import (
        thermal_balance_threshold_c,
        thermal_crouch_threshold_c,
        thermal_protect_enabled,
    )
except ImportError:
    def thermal_protect_enabled() -> bool:  # type: ignore[misc]
        return False

    def thermal_balance_threshold_c() -> int:  # type: ignore[misc]
        return 48

    def thermal_crouch_threshold_c() -> int:  # type: ignore[misc]
        return 62

LEG_MOTOR_NAMES: tuple[str, ...] = (
    "FR_0",
    "FR_1",
    "FR_2",
    "FL_0",
    "FL_1",
    "FL_2",
    "RR_0",
    "RR_1",
    "RR_2",
    "RL_0",
    "RL_1",
    "RL_2",
)

EXTRA_MOTOR_SLOTS = 20


def _temp_thresholds() -> tuple[int, int]:
    warn = int(os.environ.get("GO2_MOTOR_TEMP_WARN", "70"))
    crit = int(os.environ.get("GO2_MOTOR_TEMP_CRITICAL", "85"))
    return warn, crit


def _motor_health_level(temp_c: int, warn: int, crit: int) -> str:
    if thermal_protect_enabled() and temp_c >= thermal_crouch_threshold_c():
        return "critical"
    if thermal_protect_enabled() and temp_c >= thermal_balance_threshold_c():
        return "warn"
    if temp_c >= crit:
        return "critical"
    if temp_c >= warn:
        return "warn"
    return "ok"


def _thermal_thresholds() -> dict[str, int | None]:
    if not thermal_protect_enabled():
        return {"balance_threshold_c": None, "crouch_threshold_c": None}
    return {
        "balance_threshold_c": thermal_balance_threshold_c(),
        "crouch_threshold_c": thermal_crouch_threshold_c(),
    }


def lowstate_to_snapshot(msg: Any) -> dict[str, Any]:
    """Serializza un messaggio ``LowState_`` in JSON-friendly dict."""
    warn, crit = _temp_thresholds()
    motors: list[dict[str, Any]] = []
    max_temp = -128
    max_temp_name: str | None = None

    for i in range(EXTRA_MOTOR_SLOTS):
        m = msg.motor_state[i]
        temp = int(m.temperature)
        name = LEG_MOTOR_NAMES[i] if i < len(LEG_MOTOR_NAMES) else f"M{i}"
        if temp > max_temp:
            max_temp = temp
            max_temp_name = name
        motors.append(
            {
                "index": i,
                "name": name,
                "leg": i < len(LEG_MOTOR_NAMES),
                "mode": int(m.mode),
                "q_rad": round(float(m.q), 5),
                "dq_rad_s": round(float(m.dq), 5),
                "tau_est_nm": round(float(m.tau_est), 4),
                "temperature_c": temp,
                "lost": int(m.lost),
                "health": _motor_health_level(temp, warn, crit),
            }
        )

    b = msg.bms_state
    cells = [int(x) for x in b.cell_vol if int(x) > 0]
    imu = msg.imu_state

    overheating = any(m["health"] != "ok" for m in motors if m["leg"])
    th = _thermal_thresholds()
    balance_c = th["balance_threshold_c"]
    crouch_c = th["crouch_threshold_c"]
    above_balance = (
        balance_c is not None
        and any(int(m["temperature_c"]) >= balance_c for m in motors if m["leg"])
    )
    above_crouch = (
        crouch_c is not None
        and any(int(m["temperature_c"]) >= crouch_c for m in motors if m["leg"])
    )

    return {
        "motors": motors,
        "legs": motors[: len(LEG_MOTOR_NAMES)],
        "thermal": {
            "max_temperature_c": max_temp if max_temp_name is not None else None,
            "max_temperature_motor": max_temp_name,
            "temperature_ntc1_c": int(msg.temperature_ntc1),
            "temperature_ntc2_c": int(msg.temperature_ntc2),
            "fan_frequency_hz": [int(x) for x in msg.fan_frequency],
            "warn_threshold_c": warn,
            "critical_threshold_c": crit,
            "balance_threshold_c": balance_c,
            "crouch_threshold_c": crouch_c,
            "action_threshold_c": crouch_c,
            "above_balance_threshold": above_balance,
            "above_crouch_threshold": above_crouch,
            "above_action_threshold": above_crouch,
            "overheating_legs": overheating or above_balance,
        },
        "power": {
            "voltage_v": round(float(msg.power_v), 3),
            "current_a": round(float(msg.power_a), 3),
        },
        "bms": {
            "soc_percent": int(b.soc),
            "status_byte": int(b.status),
            "current_ma": int(b.current),
            "cycle_count": int(b.cycle),
            "bq_ntc_c": [int(x) for x in b.bq_ntc],
            "mcu_ntc_c": [int(x) for x in b.mcu_ntc],
            "cell_voltage_min_mv": min(cells) if cells else None,
            "cell_voltage_max_mv": max(cells) if cells else None,
        },
        "imu": {
            "quaternion_wxyz": [round(float(x), 5) for x in imu.quaternion],
            "gyroscope_rad_s": [round(float(x), 5) for x in imu.gyroscope],
            "temperature_c": int(imu.temperature),
        },
        "foot_force": [int(x) for x in msg.foot_force],
        "bit_flag": int(msg.bit_flag),
        "tick_us": int(msg.tick),
    }


class LowStateStore:
    """Subscriber DDS in background — mantiene l'ultimo ``LowState`` ricevuto."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, Any] | None = None
        self._count = 0
        self._first_at: float | None = None
        self._last_at: float | None = None
        self._started = False
        self._start_error: str | None = None
        self._dds_domain = int(os.environ.get("GO2_DDS_DOMAIN", "0"))
        self._dds_interface = os.environ.get("GO2_DDS_INTERFACE", "").strip()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        t = threading.Thread(target=self._run_subscriber, name="go2-lowstate-sub", daemon=True)
        t.start()

    def _run_subscriber(self) -> None:
        try:
            import sys
            from pathlib import Path

            root = Path(__file__).resolve().parent.parent
            sdk = root / "unitree_sdk2_python"
            if str(sdk) not in sys.path:
                sys.path.insert(0, str(sdk))

            from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
            from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_
        except Exception as exc:
            self._start_error = f"import failed: {exc!r}"
            return

        try:
            if self._dds_interface:
                ChannelFactoryInitialize(self._dds_domain, self._dds_interface)
            else:
                ChannelFactoryInitialize(self._dds_domain)
        except Exception as exc:
            self._start_error = f"ChannelFactoryInitialize failed: {exc!r}"
            return

        def callback(msg: Any) -> None:
            snap = lowstate_to_snapshot(msg)
            now = time.time()
            with self._lock:
                self._latest = snap
                self._count += 1
                if self._first_at is None:
                    self._first_at = now
                self._last_at = now

        try:
            sub = ChannelSubscriber("rt/lowstate", LowState_)
            sub.Init(callback, 10)
            while True:
                time.sleep(1.0)
        except Exception as exc:
            self._start_error = f"subscriber failed: {exc!r}"

    def _meta_unlocked(self) -> dict[str, Any]:
        age_s = None
        if self._last_at is not None:
            age_s = round(time.time() - self._last_at, 3)
        return {
            "connected": self._count > 0 and age_s is not None and age_s < 2.0,
            "message_count": self._count,
            "last_message_age_s": age_s,
            "first_message_at_epoch": self._first_at,
            "last_message_at_epoch": self._last_at,
            "dds_domain": self._dds_domain,
            "dds_interface": self._dds_interface or None,
            "topic": "rt/lowstate",
            "start_error": self._start_error,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._meta_unlocked()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            st = self._meta_unlocked()
            if self._latest is None:
                return {
                    "ok": False,
                    "error": st.get("start_error") or "no lowstate received yet",
                    **st,
                }
            return {"ok": True, **st, "data": self._latest}


_STORE: LowStateStore | None = None
_STORE_LOCK = threading.Lock()


def get_lowstate_store() -> LowStateStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = LowStateStore()
            _STORE.start()
            try:
                from go2_dashboard.go2_thermal_protect import attach_thermal_protector

                attach_thermal_protector(_STORE.snapshot)
            except Exception:
                pass
            try:
                from go2_dashboard.go2_battery_protect import attach_battery_protector

                attach_battery_protector(_STORE.snapshot)
            except Exception:
                pass
        return _STORE

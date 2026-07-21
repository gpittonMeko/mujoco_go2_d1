"""Watchdog batteria Go2 — a SOC critico ferma braccio, lo ripiega e manda il cane in crouch."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from go2_dashboard.go2_motor_event_log import log_motor_event


def battery_protect_enabled() -> bool:
    return os.environ.get("GO2_BATTERY_PROTECT", "1").strip().lower() in {"1", "true", "yes", "on"}


def battery_crit_soc_percent() -> int:
    # Default 10%: a 3% il bus spesso non regge più Sport/braccio e la sequenza fallisce.
    return max(1, min(50, int(os.environ.get("GO2_BATTERY_CRIT_SOC", "10"))))


def battery_clear_soc_percent() -> int:
    crit = battery_crit_soc_percent()
    clear = int(os.environ.get("GO2_BATTERY_CLEAR_SOC", "18"))
    return max(crit + 1, min(100, clear))


def battery_warn_soc_percent() -> int:
    crit = battery_crit_soc_percent()
    warn = int(os.environ.get("GO2_BATTERY_WARN_SOC", str(max(crit + 5, 20))))
    return max(crit + 1, min(100, warn))


def battery_glitch_min_voltage_v() -> float:
    """SOC 0% con bus ancora alto = tipico glitch BMS: non triggerare."""
    return float(os.environ.get("GO2_BATTERY_GLITCH_MIN_V", "26.0"))


class BatteryProtector:
    """Poll lowstate BMS: SOC ≤ soglia → stop braccio → true-zero → crouch + lock."""

    def __init__(self, snapshot_fn) -> None:
        self._snapshot_fn = snapshot_fn
        self._lock = threading.Lock()
        self._started = False
        self._state: dict[str, Any] = {
            "enabled": battery_protect_enabled(),
            "crit_soc_percent": battery_crit_soc_percent(),
            "clear_soc_percent": battery_clear_soc_percent(),
            "armed": True,
            "lock_active": False,
            "last_check_at": None,
            "last_soc_percent": None,
            "last_voltage_v": None,
            "last_trigger_at": None,
            "last_clear_at": None,
            "last_sequence": None,
            "last_error": None,
            "skipped_glitch": False,
            "last_warn_soc": None,
        }
        self._last_action_mono = 0.0
        self._sequence_running = False
        self._last_warn_logged_soc: int | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            st = dict(self._state)
        st["enabled"] = battery_protect_enabled()
        st["crit_soc_percent"] = battery_crit_soc_percent()
        st["clear_soc_percent"] = battery_clear_soc_percent()
        st["warn_soc_percent"] = battery_warn_soc_percent()
        try:
            from go2_dashboard.d1_jog.motion_guard import battery_lock_active

            st["motion_lock"] = battery_lock_active()
        except Exception:
            st["motion_lock"] = st.get("lock_active", False)
        return st

    def lock_active(self) -> bool:
        with self._lock:
            return bool(self._state.get("lock_active"))

    def start(self) -> None:
        if self._started or not battery_protect_enabled():
            return
        self._started = True
        threading.Thread(target=self._loop, name="go2-battery-protect", daemon=True).start()

    def _loop(self) -> None:
        poll_s = max(0.5, float(os.environ.get("GO2_BATTERY_POLL_S", "1.0")))
        while True:
            time.sleep(poll_s)
            if not battery_protect_enabled():
                continue
            try:
                self._tick()
            except Exception as exc:
                with self._lock:
                    self._state["last_error"] = repr(exc)
                log_motor_event("battery", f"Errore watchdog batteria: {exc!r}", level="critical")

    def _tick(self) -> None:
        snap = self._snapshot_fn()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            self._state["last_check_at"] = now_iso
            self._state["enabled"] = battery_protect_enabled()
            self._state["crit_soc_percent"] = battery_crit_soc_percent()
            self._state["clear_soc_percent"] = battery_clear_soc_percent()

        if not snap.get("ok"):
            return

        data = snap.get("data") or {}
        bms = data.get("bms") or {}
        power = data.get("power") or {}
        try:
            soc = int(bms.get("soc_percent"))
        except (TypeError, ValueError):
            return
        try:
            voltage_v = float(power.get("voltage_v") or 0.0)
        except (TypeError, ValueError):
            voltage_v = 0.0

        with self._lock:
            self._state["last_soc_percent"] = soc
            self._state["last_voltage_v"] = voltage_v
            locked = bool(self._state.get("lock_active"))

        crit = battery_crit_soc_percent()
        clear = battery_clear_soc_percent()
        warn = battery_warn_soc_percent()

        if locked:
            if soc >= clear:
                self._clear_lock(soc, voltage_v)
            return

        if soc > crit:
            with self._lock:
                self._state["skipped_glitch"] = False
            if soc <= warn and self._last_warn_logged_soc != soc:
                self._last_warn_logged_soc = soc
                with self._lock:
                    self._state["last_warn_soc"] = soc
                log_motor_event(
                    "battery",
                    f"Batteria bassa SOC {soc}% ≤ warn {warn}% — shutdown auto a ≤{crit}%",
                    level="warn",
                    detail={"soc_percent": soc, "voltage_v": voltage_v, "warn_soc_percent": warn},
                )
            elif soc > warn:
                self._last_warn_logged_soc = None
            return

        # Glitch tipico: SOC 0% ma bus ancora alto.
        if soc == 0 and voltage_v >= battery_glitch_min_voltage_v():
            with self._lock:
                already = bool(self._state.get("skipped_glitch"))
                self._state["skipped_glitch"] = True
            if not already:
                log_motor_event(
                    "battery",
                    f"SOC 0% ignorato (bus {voltage_v:.1f} V ≥ {battery_glitch_min_voltage_v():.1f} V) — possibile glitch BMS",
                    level="warn",
                )
            return

        cooldown = float(os.environ.get("GO2_BATTERY_ACTION_COOLDOWN_S", "20"))
        if self._sequence_running:
            return
        if self._last_action_mono > 0 and time.monotonic() - self._last_action_mono < cooldown:
            return

        self._run_critical_sequence(soc, voltage_v)

    def _set_lock(self, active: bool, *, reason: str) -> None:
        try:
            from go2_dashboard.d1_jog.motion_guard import clear_battery_lock, set_battery_lock

            if active:
                set_battery_lock(reason)
            else:
                clear_battery_lock(reason=reason)
        except Exception:
            pass
        with self._lock:
            self._state["lock_active"] = bool(active)

    def _clear_lock(self, soc: int, voltage_v: float) -> None:
        self._set_lock(False, reason=f"soc_recovered:{soc}")
        with self._lock:
            self._state["last_clear_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._state["armed"] = True
        log_motor_event(
            "battery",
            f"Batteria recuperata SOC {soc}% / {voltage_v:.1f} V ≥ clear {battery_clear_soc_percent()}% — lock rimosso",
            level="info",
        )

    def _run_critical_sequence(self, soc: int, voltage_v: float) -> None:
        self._sequence_running = True
        self._last_action_mono = time.monotonic()
        crit = battery_crit_soc_percent()
        log_motor_event(
            "battery",
            f"SOC {soc}% ≤ {crit}% (bus {voltage_v:.1f} V) → stop braccio, true-zero, crouch",
            level="critical",
            detail={"soc_percent": soc, "voltage_v": voltage_v, "crit_soc_percent": crit},
        )
        sequence: dict[str, Any] = {
            "soc_percent": soc,
            "voltage_v": voltage_v,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            hold = self._stop_arm()
            sequence["hold"] = hold
            zero = self._fold_arm()
            sequence["true_zero"] = zero
            crouch = self._crouch_robot()
            sequence["crouch"] = crouch
            ok = bool(crouch.get("ok")) or bool(hold.get("ok"))
            self._set_lock(True, reason=f"soc:{soc}")
            with self._lock:
                self._state["armed"] = False
                self._state["last_trigger_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self._state["last_sequence"] = sequence
                self._state["last_error"] = None if ok else str(
                    crouch.get("reason") or hold.get("reason") or "battery_sequence_partial"
                )
                self._state["skipped_glitch"] = False
            if ok:
                log_motor_event(
                    "battery",
                    f"Sequenza low-battery completata (SOC {soc}%) — lock attivo fino a ≥{battery_clear_soc_percent()}%",
                    level="critical",
                    detail=sequence,
                )
            else:
                log_motor_event(
                    "battery",
                    f"Sequenza low-battery parziale/fallita (SOC {soc}%) — lock attivo comunque",
                    level="critical",
                    detail=sequence,
                )
        except Exception as exc:
            sequence["exception"] = repr(exc)
            self._set_lock(True, reason=f"soc:{soc}:error")
            with self._lock:
                self._state["last_trigger_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self._state["last_sequence"] = sequence
                self._state["last_error"] = repr(exc)
            log_motor_event("battery", f"Sequenza low-battery eccezione: {exc!r}", level="critical", detail=sequence)
        finally:
            self._sequence_running = False

    def _stop_arm(self) -> dict[str, Any]:
        try:
            from go2_dashboard.d1_jog import service

            return service.request_emergency_hold(reason="low_battery", hard=True)
        except Exception as exc:
            return {"ok": False, "reason": repr(exc)}

    def _fold_arm(self) -> dict[str, Any]:
        try:
            from go2_dashboard.d1_jog import service

            # True-zero insegnato (posa ripiegata); fallback funcode-7 hardware zero.
            out = service.goto_true_zero_pose()
            if out.get("ok"):
                out["fold_mode"] = "true_zero"
                return out
            zero = service.go_zero()
            zero["fold_mode"] = "go_zero"
            zero["true_zero_failed"] = out
            return zero
        except Exception as exc:
            return {"ok": False, "reason": repr(exc)}

    def _crouch_robot(self) -> dict[str, Any]:
        try:
            from go2_dashboard.go2_motor_sport import invoke_sport_pose

            return invoke_sport_pose(
                "crouch",
                pre_balance_crouch=True,
                trigger="low-battery",
                arm_recovery=False,
            )
        except Exception as exc:
            return {"ok": False, "reason": repr(exc)}


_PROTECTOR: BatteryProtector | None = None
_PROTECTOR_LOCK = threading.Lock()


def attach_battery_protector(snapshot_fn) -> BatteryProtector:
    global _PROTECTOR
    with _PROTECTOR_LOCK:
        if _PROTECTOR is None:
            _PROTECTOR = BatteryProtector(snapshot_fn)
            _PROTECTOR.start()
        return _PROTECTOR


def get_battery_protector() -> BatteryProtector | None:
    with _PROTECTOR_LOCK:
        return _PROTECTOR


def battery_lock_active() -> bool:
    p = get_battery_protector()
    if p is not None and p.lock_active():
        return True
    try:
        from go2_dashboard.d1_jog.motion_guard import battery_lock_active as guard_lock

        return guard_lock()
    except Exception:
        return False

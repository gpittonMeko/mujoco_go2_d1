"""Watchdog termico Go2 — FSM su temp max: stand up → bilancio → crouch."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from go2_dashboard.go2_motor_event_log import log_motor_event
from go2_dashboard.go2_thermal_settings import (
    analyze_foot_load,
    balance_clear_threshold_c,
    crouch_recovery_threshold_c,
    crouch_stay_min_threshold_c,
    get_thermal_settings,
    leg_max_temperature_c,
    plan_weight_redistribution,
    thermal_hysteresis_c,
    thermal_threshold_band,
    update_thermal_settings,
)
from go2_dashboard.go2_thermal_runtime import (
    arm_crouch_recovery,
    clear_crouch_recovery,
    get_runtime_state,
    pending_crouch_recovery,
)


def thermal_protect_enabled() -> bool:
    return os.environ.get("GO2_THERMAL_PROTECT", "0").strip().lower() in {"1", "true", "yes", "on"}


def thermal_balance_threshold_c() -> int:
    return int(get_thermal_settings().get("balance_threshold_c", 48))


def thermal_crouch_threshold_c() -> int:
    return int(get_thermal_settings().get("crouch_threshold_c", 62))


class ThermalProtector:
    """FSM termica (temp max gambe): normal → balance → crouch, con isteresi dalle impostazioni UI."""

    def __init__(self, snapshot_fn) -> None:
        self._snapshot_fn = snapshot_fn
        self._lock = threading.Lock()
        self._started = False
        cfg = get_thermal_settings()
        initial_mode = "crouch" if pending_crouch_recovery() else "normal"
        self._state: dict[str, Any] = {
            "enabled": thermal_protect_enabled(),
            "balance_threshold_c": thermal_balance_threshold_c(),
            "crouch_threshold_c": thermal_crouch_threshold_c(),
            "threshold_c": thermal_crouch_threshold_c(),
            "balance_cooldown_s": float(os.environ.get("GO2_THERMAL_BALANCE_COOLDOWN_S", "30")),
            "cooldown_s": float(os.environ.get("GO2_THERMAL_COOLDOWN_S", "30")),
            "armed": True,
            "last_check_at": None,
            "last_balance_at": None,
            "last_balance_motors": [],
            "last_trigger_at": None,
            "last_trigger_motors": [],
            "last_recovery_at": None,
            "last_sport_result": None,
            "last_error": None,
            "weight_hint": None,
            "weight_plan_now": None,
            "warm_motors_now": [],
            "critical_motors_now": [],
            "hot_motors_now": [],
            "max_leg_temp_c": 0,
            "max_leg_temp_motor": None,
            "settings": cfg,
            "thermal_mode": initial_mode,
        }
        self._last_balance_mono = 0.0
        self._last_crouch_mono = 0.0
        self._last_recovery_mono = 0.0
        self._last_return_stand_mono = 0.0
        self._last_foot_force: list[int] = []
        self._last_warm_logged: tuple[str, ...] = ()
        self._last_critical_logged: tuple[str, ...] = ()
        if initial_mode == "crouch":
            self._awaiting_crouch_recovery = True
        else:
            self._awaiting_crouch_recovery = False

    def _mode(self) -> str:
        with self._lock:
            return str(self._state.get("thermal_mode") or "normal")

    def _set_mode(self, mode: str) -> None:
        mode = mode if mode in {"normal", "balance", "crouch"} else "normal"
        with self._lock:
            self._state["thermal_mode"] = mode

    def status(self) -> dict[str, Any]:
        with self._lock:
            st = dict(self._state)
            st["awaiting_crouch_recovery"] = self._awaiting_crouch_recovery or pending_crouch_recovery()
        st["thermal_runtime"] = get_runtime_state()
        st["settings"] = get_thermal_settings()
        st.update(thermal_threshold_band())
        st["threshold_c"] = st["crouch_threshold_c"]
        return st

    def apply_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        prev = get_thermal_settings()
        updated = update_thermal_settings(patch)
        if (
            int(prev.get("crouch_threshold_c", 0)) != int(updated.get("crouch_threshold_c", 0))
            or int(prev.get("balance_threshold_c", 0)) != int(updated.get("balance_threshold_c", 0))
            or int(prev.get("threshold_hysteresis_c", 0)) != int(updated.get("threshold_hysteresis_c", 0))
        ):
            self._last_crouch_mono = 0.0
            self._last_balance_mono = 0.0
            log_motor_event(
                "thermal",
                "Soglie aggiornate — cooldown crouch/bilancio azzerato (crouch immediato se temp max ≥ soglia)",
                level="info",
            )
        with self._lock:
            self._state["settings"] = updated
            self._state["balance_threshold_c"] = thermal_balance_threshold_c()
            self._state["crouch_threshold_c"] = thermal_crouch_threshold_c()
            self._state["threshold_c"] = thermal_crouch_threshold_c()
        return updated

    def _crouch_cooldown_elapsed(self) -> bool:
        """Da stand/bilancio → crouch: attesa breve; ripetizione crouch: cooldown pieno."""
        mode = self._mode()
        if mode in {"normal", "balance"}:
            cd = float(os.environ.get("GO2_THERMAL_CROUCH_ESCALATION_COOLDOWN_S", "30"))
        else:
            cd = float(self._state["cooldown_s"])
        if self._last_crouch_mono <= 0:
            return True
        return time.monotonic() - self._last_crouch_mono >= cd

    def preview_weight_plan(self, motors: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        with self._lock:
            foot = list(self._last_foot_force)
            hot = motors if motors is not None else list(self._state.get("warm_motors_now") or [])
        return plan_weight_redistribution(foot, hot, get_thermal_settings())

    def run_balance_now(self) -> dict[str, Any]:
        from go2_dashboard.go2_motor_sport import invoke_thermal_balance

        with self._lock:
            foot = list(self._last_foot_force)
            warm = list(self._state.get("warm_motors_now") or [])
        result = invoke_thermal_balance(
            foot_force=foot,
            warm_motors=warm,
            settings=get_thermal_settings(),
            trigger="manuale",
        )
        if result.get("ok"):
            self._set_mode("balance")
        return result

    def run_crouch_recovery_now(self) -> dict[str, Any]:
        """Autobilanciamento forzato dopo crouch — ignora soglia crouch−isteresi (pulsante manuale)."""
        if not pending_crouch_recovery() and self._mode() != "crouch":
            arm_crouch_recovery(trigger="manuale-recovery")
        self._awaiting_crouch_recovery = True
        self._set_mode("crouch")
        self._last_recovery_mono = 0.0
        with self._lock:
            warm = list(self._state.get("warm_motors_now") or [])
            foot = list(self._last_foot_force)
        self._trigger_crouch_to_balance(
            warm,
            foot,
            crouch_recovery_threshold_c(),
            thermal_hysteresis_c(),
            thermal_crouch_threshold_c(),
            trigger="manuale-recovery",
            max_temp=None,
            max_motor=None,
        )
        with self._lock:
            result = dict(self._state.get("last_sport_result") or {})
        result.setdefault("mode", "thermal_balance")
        return result

    def start(self) -> None:
        if self._started or not thermal_protect_enabled():
            return
        self._started = True
        threading.Thread(target=self._loop, name="go2-thermal-protect", daemon=True).start()

    def _loop(self) -> None:
        poll_s = max(0.5, float(os.environ.get("GO2_THERMAL_POLL_S", "1.0")))
        while True:
            time.sleep(poll_s)
            if not thermal_protect_enabled():
                continue
            try:
                self._tick()
            except Exception as exc:
                with self._lock:
                    self._state["last_error"] = repr(exc)
                log_motor_event("error", f"Errore watchdog termico: {exc!r}", level="critical")

    def _tick(self) -> None:
        snap = self._snapshot_fn()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
        balance_c = thermal_balance_threshold_c()
        crouch_c = thermal_crouch_threshold_c()
        hysteresis_c = thermal_hysteresis_c()
        balance_clear_c = balance_clear_threshold_c()
        crouch_recover_c = crouch_recovery_threshold_c()
        crouch_stay_min_c = crouch_stay_min_threshold_c()

        with self._lock:
            self._state["last_check_at"] = now_iso
            self._state["settings"] = get_thermal_settings()
            self._state.update(thermal_threshold_band())

        if not snap.get("ok"):
            with self._lock:
                self._state["warm_motors_now"] = []
                self._state["critical_motors_now"] = []
                self._state["hot_motors_now"] = []
                self._state["weight_plan_now"] = None
                self._state["max_leg_temp_c"] = 0
                self._state["max_leg_temp_motor"] = None
            return

        data = snap.get("data") or {}
        legs = data.get("legs") or []
        foot_force = list(data.get("foot_force") or [])
        self._last_foot_force = foot_force

        max_temp, max_motor = leg_max_temperature_c(legs)
        warm = [
            {"name": str(m.get("name")), "temperature_c": int(m.get("temperature_c", 0))}
            for m in legs
            if int(m.get("temperature_c", 0)) >= balance_c
        ]
        critical = [
            {"name": str(m.get("name")), "temperature_c": int(m.get("temperature_c", 0))}
            for m in legs
            if int(m.get("temperature_c", 0)) >= crouch_c
        ]

        load = analyze_foot_load(foot_force)
        plan = plan_weight_redistribution(foot_force, warm, get_thermal_settings())
        with self._lock:
            self._state["weight_hint"] = load
            self._state["weight_plan_now"] = plan
            self._state["warm_motors_now"] = warm
            self._state["critical_motors_now"] = critical
            self._state["hot_motors_now"] = critical or warm
            self._state["max_leg_temp_c"] = max_temp
            self._state["max_leg_temp_motor"] = max_motor

        mode = self._mode()
        self._log_threshold_crossings(warm, critical, balance_c, crouch_c, max_temp, max_motor, mode)
        if pending_crouch_recovery() and mode != "crouch":
            self._awaiting_crouch_recovery = True
            self._set_mode("crouch")

        if mode == "crouch":
            self._tick_crouch(
                max_temp,
                max_motor,
                warm,
                foot_force,
                crouch_recover_c,
                crouch_stay_min_c,
                crouch_c,
                hysteresis_c,
            )
            return

        if mode == "balance":
            self._tick_balance(
                max_temp,
                max_motor,
                warm,
                foot_force,
                balance_clear_c,
                balance_c,
                crouch_c,
                hysteresis_c,
            )
            return

        self._tick_normal(max_temp, max_motor, warm, critical, foot_force, balance_c, crouch_c)

    def _log_threshold_crossings(
        self,
        warm: list[dict[str, Any]],
        critical: list[dict[str, Any]],
        balance_c: int,
        crouch_c: int,
        max_temp: int,
        max_motor: str | None,
        mode: str,
    ) -> None:
        warm_key = tuple(f"{m['name']}:{m['temperature_c']}" for m in warm)
        crit_key = tuple(f"{m['name']}:{m['temperature_c']}" for m in critical)
        if mode != "crouch" and warm and warm_key != self._last_warm_logged:
            from go2_dashboard.go2_motor_event_log import log_thermal_watch

            log_thermal_watch(
                "warn",
                warm,
                balance_c,
                max_temp_c=max_temp,
                max_motor=max_motor,
            )
            self._last_warm_logged = warm_key
        if not warm:
            self._last_warm_logged = ()
        if critical and crit_key != self._last_critical_logged:
            from go2_dashboard.go2_motor_event_log import log_thermal_watch

            log_thermal_watch(
                "critical",
                critical,
                crouch_c,
                kind="crouch",
                max_temp_c=max_temp,
                max_motor=max_motor,
            )
            self._last_critical_logged = crit_key
        if not critical:
            self._last_critical_logged = ()

    def _recovery_enabled(self) -> bool:
        if not bool(get_thermal_settings().get("recovery_stand_enabled", True)):
            return False
        try:
            from go2_dashboard.go2_battery_protect import battery_lock_active

            if battery_lock_active():
                return False
        except Exception:
            pass
        return True

    def _tick_crouch(
        self,
        max_temp: int,
        max_motor: str | None,
        warm: list[dict[str, Any]],
        foot_force: list[int],
        crouch_recover_c: int,
        crouch_stay_min_c: int,
        crouch_c: int,
        hysteresis_c: int,
    ) -> None:
        """Crouch: resta finché max > crouch_recover; a max ≤ crouch_recover (crouch−isteresi) → autobilanciamento."""
        if max_temp > crouch_recover_c:
            return
        if not self._recovery_enabled():
            return
        min_after_crouch_s = float(os.environ.get("GO2_THERMAL_CROUCH_RECOVERY_MIN_S", "1"))
        if time.monotonic() - self._last_crouch_mono < min_after_crouch_s and self._last_crouch_mono > 0:
            return
        recovery_cd = float(os.environ.get("GO2_THERMAL_CROUCH_RECOVERY_COOLDOWN_S", "3"))
        if time.monotonic() - self._last_recovery_mono < recovery_cd:
            return
        log_motor_event(
            "balance",
            f"Temp max {max_temp}°C ({max_motor or '—'}) ≤ {crouch_recover_c}°C "
            f"(crouch {crouch_c}°C − isteresi {hysteresis_c}°C) → autobilanciamento",
            level="info",
        )
        self._trigger_crouch_to_balance(
            warm,
            foot_force,
            crouch_recover_c,
            hysteresis_c,
            crouch_c,
            trigger="auto-termico",
            max_temp=max_temp,
            max_motor=max_motor,
        )

    def _tick_balance(
        self,
        max_temp: int,
        max_motor: str | None,
        warm: list[dict[str, Any]],
        foot_force: list[int],
        balance_clear_c: int,
        balance_c: int,
        crouch_c: int,
        hysteresis_c: int,
    ) -> None:
        """Autobilanciamento: max ≥ crouch → crouch; max ≤ balance_clear → stand up; altrimenti resta."""
        if max_temp >= crouch_c:
            if self._crouch_cooldown_elapsed():
                crit_list = warm if warm else [{"name": max_motor or "?", "temperature_c": max_temp}]
                self._trigger_crouch(crit_list, foot_force)
            return
        if max_temp <= balance_clear_c:
            if not self._recovery_enabled():
                self._set_mode("normal")
                return
            return_cd = float(os.environ.get("GO2_THERMAL_RETURN_STAND_COOLDOWN_S", "3"))
            if time.monotonic() - self._last_return_stand_mono < return_cd:
                return
            log_motor_event(
                "recovery",
                f"Temp max {max_temp}°C ({max_motor or '—'}) ≤ {balance_clear_c}°C "
                f"(bilancio {balance_c}°C − isteresi {hysteresis_c}°C) → stand up",
                level="info",
            )
            self._trigger_return_to_stand_from_balance(balance_clear_c, balance_c, hysteresis_c)
            return
        # max tra balance_clear+1 e crouch-1: resta in autobilanciamento (es. 42–61°C)

    def _tick_normal(
        self,
        max_temp: int,
        max_motor: str | None,
        warm: list[dict[str, Any]],
        critical: list[dict[str, Any]],
        foot_force: list[int],
        balance_c: int,
        crouch_c: int,
    ) -> None:
        """Stand up: max ≥ crouch → crouch; max ≥ balance → autobilanciamento."""
        if max_temp >= crouch_c:
            if self._crouch_cooldown_elapsed():
                self._trigger_crouch(critical or warm, foot_force)
            return
        if max_temp >= balance_c:
            cooldown = float(self._state["balance_cooldown_s"])
            if time.monotonic() - self._last_balance_mono >= cooldown:
                log_motor_event(
                    "balance",
                    f"Temp max {max_temp}°C ({max_motor or '—'}) ≥ {balance_c}°C → autobilanciamento",
                    level="info",
                )
                self._trigger_balance(warm, foot_force)
            return

    def _trigger_crouch_to_balance(
        self,
        warm_motors: list[dict[str, Any]],
        foot_force: list[int],
        crouch_recover_c: int,
        hysteresis_c: int,
        crouch_c: int,
        *,
        trigger: str,
        max_temp: int | None = None,
        max_motor: str | None = None,
    ) -> None:
        from go2_dashboard.go2_motor_event_log import log_crouch_to_balance_recovery
        from go2_dashboard.go2_motor_sport import invoke_thermal_balance

        settings = get_thermal_settings()
        result = invoke_thermal_balance(
            foot_force=foot_force,
            warm_motors=warm_motors,
            settings=settings,
            trigger=trigger,
            after_crouch_recovery=True,
        )
        log_crouch_to_balance_recovery(
            result,
            trigger=trigger,
            recovery_threshold_c=crouch_recover_c,
            crouch_threshold_c=crouch_c,
            hysteresis_c=hysteresis_c,
            max_temp_c=max_temp,
            max_motor=max_motor,
        )
        self._last_recovery_mono = time.monotonic()
        with self._lock:
            self._state["last_recovery_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._state["last_sport_result"] = result
            self._state["last_error"] = None if result.get("ok") else str(result.get("reason"))
        if result.get("ok"):
            self._awaiting_crouch_recovery = False
            clear_crouch_recovery(reason="auto_crouch_to_balance")
            self._last_crouch_mono = 0.0
            self._last_balance_mono = time.monotonic()
            names = [str(m.get("name")) for m in warm_motors]
            with self._lock:
                self._state["thermal_mode"] = "balance"
                self._state["last_balance_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self._state["last_balance_motors"] = names
            log_motor_event(
                "balance",
                f"Stato → AUTOBILANCIAMENTO (post-crouch, temp max {max_temp}°C)",
                level="info",
            )
        else:
            self._awaiting_crouch_recovery = True
            with self._lock:
                self._state["thermal_mode"] = "crouch"
            hint = str(result.get("hint") or result.get("reason") or "sport_failed")
            log_motor_event(
                "balance",
                f"Autobilanciamento post-crouch fallito — cane resta accucciato — {hint}",
                level="critical",
                detail=result,
            )

    def _trigger_return_to_stand_from_balance(
        self,
        balance_clear_c: int,
        balance_c: int,
        hysteresis_c: int,
    ) -> None:
        from go2_dashboard.go2_motor_sport import invoke_thermal_return_to_stand

        result = invoke_thermal_return_to_stand(
            clear_threshold_c=balance_clear_c,
            balance_threshold_c=balance_c,
            hysteresis_c=hysteresis_c,
            trigger="auto-termico",
        )
        self._last_return_stand_mono = time.monotonic()
        with self._lock:
            self._state["last_sport_result"] = result
            self._state["last_error"] = None if result.get("ok") else str(result.get("reason"))
        if result.get("ok"):
            with self._lock:
                self._state["thermal_mode"] = "normal"
                self._state["last_recovery_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    def _trigger_balance(self, warm_motors: list[dict[str, Any]], foot_force: list[int]) -> None:
        from go2_dashboard.go2_motor_sport import invoke_thermal_balance

        settings = get_thermal_settings()
        result = invoke_thermal_balance(
            foot_force=foot_force,
            warm_motors=warm_motors,
            settings=settings,
            trigger="auto-termico",
        )
        self._last_balance_mono = time.monotonic()
        names = [str(m.get("name")) for m in warm_motors]
        with self._lock:
            self._state["thermal_mode"] = "balance"
            self._state["last_balance_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._state["last_balance_motors"] = names
            self._state["last_sport_result"] = result
            self._state["last_error"] = None if result.get("ok") else str(result.get("reason"))
        if not result.get("ok"):
            with self._lock:
                self._state["thermal_mode"] = "normal"

    def _trigger_crouch(self, critical_motors: list[dict[str, Any]], foot_force: list[int]) -> None:
        from go2_dashboard.go2_motor_sport import invoke_thermal_crouch

        settings = get_thermal_settings()
        result = invoke_thermal_crouch(
            foot_force=foot_force,
            hot_motors=critical_motors,
            settings=settings,
            trigger="auto-termico",
        )
        self._last_crouch_mono = time.monotonic()
        crouch_step = (result.get("steps") or {}).get("crouch") if isinstance(result.get("steps"), dict) else None
        crouch_ok = bool((crouch_step or {}).get("ok")) or bool(result.get("ok"))
        if crouch_ok:
            self._awaiting_crouch_recovery = True
            arm_crouch_recovery(trigger="auto-termico", motors=critical_motors)
        names = [str(m.get("name")) for m in critical_motors]
        with self._lock:
            self._state["thermal_mode"] = "crouch" if crouch_ok else self._state.get("thermal_mode", "normal")
            self._state["last_trigger_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._state["last_trigger_motors"] = names
            self._state["last_sport_result"] = result
            self._state["last_error"] = None if result.get("ok") else str(result.get("reason"))
            self._state["awaiting_crouch_recovery"] = self._awaiting_crouch_recovery


_PROTECTOR: ThermalProtector | None = None
_PROTECTOR_LOCK = threading.Lock()


def attach_thermal_protector(snapshot_fn) -> ThermalProtector:
    global _PROTECTOR
    with _PROTECTOR_LOCK:
        if _PROTECTOR is None:
            _PROTECTOR = ThermalProtector(snapshot_fn)
            _PROTECTOR.start()
        return _PROTECTOR

"""Comandi Sport (stand / crouch / bilanciamento) per motor health."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from go2_dashboard.go2_motor_event_log import (
    log_balance_stand,
    log_crouch,
    log_manual_pose,
    log_crouch_to_balance_recovery,
    log_return_to_stand_from_balance,
    log_weight_shift,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

LAST_MANUAL_SPORT: dict[str, Any] = {
    "updated_at": None,
    "mode": None,
    "result": None,
    "error": None,
}
_LAST_SPORT_LOCK = threading.Lock()
_SPORT_SUBPROCESS_LOCK = threading.Lock()
_SPORT_READY_LOCK = threading.Lock()
_SPORT_READY_CACHE: dict[str, Any] = {"mono": 0.0, "probe": {}}
_LAST_SPORT_SKIP_LOG_MONO = 0.0


def _parse_subprocess_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {"ok": False, "reason": "empty_stdout"}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return {"ok": False, "reason": "bad_json", "stdout_tail": text[-800:]}


def sport_motion_allowed(*, mode: str | None = None) -> tuple[bool, str | None]:
    if os.environ.get("GO2_ENABLE_BASE_MOTION", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return False, "GO2_ENABLE_BASE_MOTION=1 richiesto sulla NX."
    if os.environ.get("GO2_LOCAL", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return False, "GO2_LOCAL=1 richiesto (server sulla Jetson)."
    # Lock batteria: consente solo crouch (messa in sicurezza), niente stand/move/balance.
    mode_l = (mode or "").strip().lower()
    if mode_l and mode_l != "crouch":
        try:
            from go2_dashboard.go2_battery_protect import battery_lock_active

            if battery_lock_active():
                return False, "battery_critical_lock — ricaricare prima di stand/move/balance."
        except Exception:
            pass
    return True, None


def last_manual_sport_status() -> dict[str, Any]:
    with _LAST_SPORT_LOCK:
        return dict(LAST_MANUAL_SPORT)


def _sport_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GO2_LOCAL", "1")
    env.setdefault("GO2_ENABLE_BASE_MOTION", "1")
    env.setdefault("GO2_DDS_DOMAIN", "0")
    env.setdefault("GO2_DDS_INTERFACE", "eth0")
    env.setdefault("GO2_SPORT_MOTION_PREPARE", "1")
    env.setdefault("GO2_SPORT_RELEASE_IF_HELD", "0")
    env.setdefault("GO2_SPORT_SELECT_MODE", "none")
    env.setdefault("GO2_SPORT_ENABLE_LEASE", "0")
    env.setdefault("GO2_SPORT_AUTO_SERVICE_SWITCH", "1")
    env.setdefault("GO2_SPORT_SELECT_SETTLE_S", "0.5")
    env["PYTHONUNBUFFERED"] = "1"
    root = str(PROJECT_ROOT)
    scripts = str(PROJECT_ROOT / "scripts")
    sdk = str(PROJECT_ROOT / "unitree_sdk2_python")
    parts = [p for p in (root, scripts, sdk, env.get("PYTHONPATH", "")) if p]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(os.pathsep.join(parts).split(os.pathsep)))
    return env


def probe_sport_service_ready(*, force: bool = False) -> dict[str, Any]:
    """CheckMode + Sport API version (subprocess). Risultato in cache ~60s."""
    ttl = float(os.environ.get("GO2_SPORT_READY_PROBE_TTL_S", "60"))
    with _SPORT_READY_LOCK:
        if not force and time.monotonic() - float(_SPORT_READY_CACHE.get("mono") or 0) < ttl:
            return dict(_SPORT_READY_CACHE.get("probe") or {})
    timeout_s = float(os.environ.get("GO2_SPORT_READY_PROBE_TIMEOUT_S", "25"))
    raw = _run_sport_script(["--mode", "dds_ping"], _sport_env(), timeout_s)
    mode_result = raw.get("motion_mode_result") if isinstance(raw.get("motion_mode_result"), dict) else {}
    mode_name = str(mode_result.get("name") or "").strip()
    sport_ok = int(raw.get("sport_api_version_code") or -1) == 0
    ready = bool(raw.get("ok")) or sport_ok
    hint = "Sport pronto via DDS."
    if not ready:
        if not mode_name:
            hint = (
                "Sport non attivo: CheckMode name vuoto — app Unitree Go2 → Sport/AI → Stand up, "
                "poi chiudi app e riprova Test DDS."
            )
        else:
            hint = (
                f"Modalità «{mode_name}» rilevata ma servizio Sport non risponde (3102/3104). "
                "Chiudi app Unitree e dashboard :5052."
            )
    probe = {
        "ready": ready,
        "motion_mode": mode_name or None,
        "sport_api_ok": sport_ok,
        "dds_ping": raw,
        "hint_it": hint,
    }
    with _SPORT_READY_LOCK:
        _SPORT_READY_CACHE["mono"] = time.monotonic()
        _SPORT_READY_CACHE["probe"] = probe
    return probe


def _gate_sport_or_block(mode: str, *, trigger: str) -> dict[str, Any] | None:
    """Salta comandi auto-termico se Sport non è raggiungibile (evita spam 3102)."""
    global _LAST_SPORT_SKIP_LOG_MONO
    if os.environ.get("GO2_SPORT_REQUIRE_READY", "1").lower() in {"0", "false", "no", "off"}:
        return None
    if trigger == "manuale":
        return None
    probe = probe_sport_service_ready()
    if probe.get("ready"):
        return None
    skip_log_s = float(os.environ.get("GO2_THERMAL_SPORT_SKIP_LOG_S", "180"))
    if time.monotonic() - _LAST_SPORT_SKIP_LOG_MONO >= skip_log_s:
        from go2_dashboard.go2_motor_event_log import log_motor_event

        log_motor_event(
            "sport",
            f"[{trigger}] Autotermico saltato ({mode}) — {probe.get('hint_it')}",
            level="warn",
            detail=probe,
        )
        _LAST_SPORT_SKIP_LOG_MONO = time.monotonic()
    return {
        "ok": False,
        "mode": mode,
        "reason": "sport_service_unavailable",
        "sport_probe": probe,
        "hint": probe.get("hint_it"),
    }


def invoke_dds_sport_ping() -> dict[str, Any]:
    """CheckMode MotionSwitcher in subprocess (isolato dal subscriber LowState)."""
    timeout_s = float(os.environ.get("GO2_SPORT_RPC_TIMEOUT_S", "55"))
    result = _run_sport_script(["--mode", "dds_ping"], _sport_env(), timeout_s)

    from go2_dashboard.go2_motor_event_log import log_motor_event

    msg = result.get("hint_it") or ("DDS Sport OK" if result.get("ok") else "DDS Sport fallito")
    if not result.get("ok"):
        mode_result = result.get("motion_mode_result") or {}
        mode_name = (mode_result.get("name") or "") if isinstance(mode_result, dict) else ""
        if result.get("sport_api_version_code") == 3102:
            if not mode_name:
                msg += (
                    " — servizio Sport non attivo sul robot (CheckMode name vuoto). "
                    "Apri app Unitree Go2 → modalità Sport/AI, oppure chiudi l'app e riprova."
                )
            else:
                msg += f" — modalità corrente «{mode_name}» ma Sport non risponde; chiudere app Unitree."
        if result.get("sport_lease", {}).get("enabled") and not result["sport_lease"].get("applied"):
            msg += " Lease Sport non acquisito (app telefono?)."
        if result.get("sport_api_version_meaning"):
            msg += f" — Sport: {result['sport_api_version_meaning']}"
        elif result.get("motion_switcher_check_meaning"):
            msg += f" — {result['motion_switcher_check_meaning']}"
        elif result.get("stderr_tail"):
            msg += f" — stderr: {str(result['stderr_tail'])[:240]}"
        elif result.get("stdout_tail"):
            msg += f" — stdout: {str(result['stdout_tail'])[:240]}"
        elif result.get("error"):
            msg += f" — {result['error']}"
    log_motor_event(
        "dds",
        f"[test] {msg} (domain={os.environ.get('GO2_DDS_DOMAIN', '0')}, iface={os.environ.get('GO2_DDS_INTERFACE', 'eth0')})",
        level="info" if result.get("ok") else "critical",
        detail=result,
    )
    return result


def _run_sport_script(argv: list[str], env: dict[str, str], timeout_s: float) -> dict[str, Any]:
    script = PROJECT_ROOT / "scripts" / "sport_accompany_once.py"
    if not script.is_file():
        return {"ok": False, "reason": "missing scripts/sport_accompany_once.py"}
    with _SPORT_SUBPROCESS_LOCK:
        try:
            proc = subprocess.run(
                [sys.executable, str(script), *argv],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=env,
            )
            result = _parse_subprocess_json(proc.stdout or "")
            if proc.stderr:
                result.setdefault("stderr_tail", proc.stderr[-800:])
            if proc.returncode != 0 and not result.get("ok"):
                result["subprocess_returncode"] = proc.returncode
            return result
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": f"timeout_after_{timeout_s}s"}
        except Exception as exc:
            return {"ok": False, "reason": repr(exc)}


def invoke_sport_pose(
    mode: str,
    *,
    pre_balance_crouch: bool = False,
    trigger: str = "manuale",
    arm_recovery: bool = True,
) -> dict[str, Any]:
    mode = str(mode).strip().lower()
    if mode not in {"stand_up", "crouch"}:
        return {"ok": False, "reason": f"mode invalido: {mode!r}"}

    ok_gate, reason = sport_motion_allowed(mode=mode)
    if not ok_gate:
        return {"ok": False, "reason": reason}

    env = _sport_env()
    if mode == "crouch" and pre_balance_crouch:
        env["GO2_CROUCH_PRE_BALANCE"] = "1"
        env.setdefault("GO2_CROUCH_BALANCE_SETTLE_S", "0.8")

    timeout_s = float(os.environ.get("GO2_SPORT_RPC_TIMEOUT_S", "55"))
    result = _run_sport_script(["--mode", mode, "--enable", "1"], env, timeout_s)
    result.setdefault("mode", mode)

    if mode == "crouch" and result.get("ok") and arm_recovery:
        try:
            from go2_dashboard.go2_thermal_runtime import arm_crouch_recovery

            arm_crouch_recovery(trigger=trigger)
        except Exception:
            pass
    elif mode == "stand_up" and result.get("ok"):
        try:
            from go2_dashboard.go2_thermal_runtime import clear_crouch_recovery

            clear_crouch_recovery(reason=f"stand_up_{trigger}")
        except Exception:
            pass

    if trigger == "manuale":
        log_manual_pose(mode, result)

    with _LAST_SPORT_LOCK:
        LAST_MANUAL_SPORT["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        LAST_MANUAL_SPORT["mode"] = mode
        LAST_MANUAL_SPORT["result"] = result
        LAST_MANUAL_SPORT["error"] = None if result.get("ok") else str(result.get("reason") or result.get("hint"))
    return result


def invoke_sport_balance(*, trigger: str = "termico") -> dict[str, Any]:
    ok_gate, reason = sport_motion_allowed(mode="balance")
    if not ok_gate:
        return {"ok": False, "reason": reason}
    timeout_s = float(os.environ.get("GO2_SPORT_RPC_TIMEOUT_S", "55"))
    result = _run_sport_script(["--mode", "balance"], _sport_env(), timeout_s)
    result.setdefault("mode", "balance")
    log_balance_stand(result, trigger=trigger)
    return result


def invoke_sport_move(
    vx: float,
    vy: float,
    vyaw: float = 0.0,
    *,
    duration_s: float = 0.65,
    trigger: str = "termico",
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ok_gate, reason = sport_motion_allowed(mode="move")
    if not ok_gate:
        return {"ok": False, "reason": reason}

    timeout_s = float(os.environ.get("GO2_SPORT_RPC_TIMEOUT_S", "55"))
    argv = [
        "--mode", "move",
        "--vx", str(vx), "--vy", str(vy), "--vyaw", str(vyaw),
        "--duration", str(duration_s),
    ]
    result = _run_sport_script(argv, _sport_env(), timeout_s)
    result.setdefault("mode", "move")
    if plan:
        log_weight_shift(plan, result, trigger=trigger)
    return result


def _ensure_standing_for_balance(
    steps: dict[str, Any],
    *,
    trigger: str,
    from_crouch_recovery: bool = False,
) -> bool:
    """StandUp prima del bilanciamento — Move/BalanceStand non agiscono da crouch."""
    from go2_dashboard.go2_motor_event_log import log_crouch_recovery_stand_up

    stand = invoke_sport_pose("stand_up", trigger=trigger)
    steps["stand_up"] = stand
    if from_crouch_recovery:
        log_crouch_recovery_stand_up(stand, trigger=trigger)
    if not stand.get("ok"):
        return False
    if from_crouch_recovery:
        settle_s = float(os.environ.get("GO2_CROUCH_RECOVERY_STAND_SETTLE_S", "2.0"))
    else:
        settle_s = float(os.environ.get("GO2_BALANCE_STAND_SETTLE_S", "1.0"))
    if settle_s > 0:
        time.sleep(settle_s)
        steps["stand_settle_s"] = settle_s
    return True


def _apply_weight_plan(
    foot_force: list[int],
    motors: list[dict[str, Any]],
    settings: dict[str, Any],
    steps: dict[str, Any],
    *,
    trigger: str,
    force_hot_bias: bool = False,
) -> dict[str, Any] | None:
    from go2_dashboard.go2_thermal_settings import plan_weight_redistribution

    plan = plan_weight_redistribution(
        foot_force, motors, settings, force_hot_bias=force_hot_bias
    )
    if not plan:
        return None
    shift = invoke_sport_move(
        plan["vx"],
        plan["vy"],
        plan.get("vyaw", 0.0),
        duration_s=float(plan["duration_s"]),
        trigger=trigger,
        plan=plan,
    )
    steps["weight_shift"] = {**shift, "plan": plan}
    settle_s = float(plan.get("settle_s") or 0)
    if shift.get("ok") and settle_s > 0:
        time.sleep(settle_s)
        steps["weight_shift"]["settle_s"] = settle_s
    return plan


def invoke_thermal_balance(
    *,
    foot_force: list[int],
    warm_motors: list[dict[str, Any]],
    settings: dict[str, Any],
    trigger: str = "auto-termico",
    after_crouch_recovery: bool = False,
) -> dict[str, Any]:
    """Autobilanciamento + BalanceStand (soglia bilancio UI), senza crouch."""
    manual = trigger == "manuale"
    if not manual and not after_crouch_recovery:
        blocked = _gate_sport_or_block("thermal_balance", trigger=trigger)
        if blocked:
            return blocked
    steps: dict[str, Any] = {}
    if manual or after_crouch_recovery:
        stand_trigger = "bilanciamento" if manual else trigger
        if not _ensure_standing_for_balance(
            steps,
            trigger=stand_trigger,
            from_crouch_recovery=after_crouch_recovery,
        ):
            return {
                "ok": False,
                "mode": "thermal_balance",
                "steps": steps,
                "reason": "stand_up_failed",
                "hint": "Stand up fallito — impossibile bilanciare da postura accucciata.",
            }

    plan = None
    force_hot = bool(warm_motors) and (manual or after_crouch_recovery)
    if settings.get("weight_balance_enabled"):
        plan = _apply_weight_plan(
            foot_force,
            warm_motors,
            settings,
            steps,
            trigger=trigger,
            force_hot_bias=force_hot,
        )
    balance = invoke_sport_balance(trigger=trigger)
    steps["balance_stand"] = balance
    stand_step = steps.get("stand_up")
    stand_ok = bool(stand_step.get("ok")) if isinstance(stand_step, dict) else (not after_crouch_recovery)
    shift_ok = not plan or steps.get("weight_shift", {}).get("ok")
    ok = stand_ok and balance.get("ok") and shift_ok

    if manual:
        if plan and plan.get("reasons"):
            hint = "Stand up + spostamento peso (" + ", ".join(plan["reasons"]) + ") + BalanceStand."
        elif warm_motors:
            hint = (
                "Stand up + BalanceStand — carico zampa già in banda e motori caldi simmetrici; "
                "nessuno spostamento applicato."
            )
        else:
            hint = (
                "Stand up + BalanceStand — nessun motore sopra soglia bilanciamento; "
                "solo stabilizzazione postura."
            )
        if plan and not shift_ok:
            hint = "Stand up eseguito ma spostamento peso fallito — vedi log eventi Sport."
    elif after_crouch_recovery:
        if plan and plan.get("reasons"):
            hint = "Recupero crouch: spostamento peso (" + ", ".join(plan["reasons"]) + ") + BalanceStand."
        elif warm_motors:
            hint = "Recupero crouch: BalanceStand — motori ancora sopra soglia bilanciamento."
        else:
            hint = "Recupero crouch: BalanceStand — ritorno postura autobilanciamento."
    else:
        hint = "Autobilanciamento peso + BalanceStand (stabilizzazione temperature)."

    return {
        "ok": ok,
        "mode": "thermal_balance",
        "steps": steps,
        "weight_plan": plan,
        "warm_motors": warm_motors,
        "hint": hint,
    }


def invoke_thermal_crouch(
    *,
    foot_force: list[int],
    hot_motors: list[dict[str, Any]],
    settings: dict[str, Any],
    trigger: str = "auto-termico",
) -> dict[str, Any]:
    """Crouch termico (soglia ~62°C), con eventuale spostamento peso prima."""
    blocked = _gate_sport_or_block("thermal_crouch", trigger=trigger)
    if blocked:
        return blocked
    steps: dict[str, Any] = {}
    plan = None
    if settings.get("weight_balance_enabled"):
        plan = _apply_weight_plan(foot_force, hot_motors, settings, steps, trigger=trigger)

    crouch = invoke_sport_pose(
        "crouch",
        pre_balance_crouch=bool(settings.get("pre_balance_crouch", True)),
        trigger=trigger,
    )
    steps["crouch"] = crouch
    log_crouch(hot_motors, crouch, trigger=trigger)
    shift_ok = not plan or steps.get("weight_shift", {}).get("ok")
    ok = bool(crouch.get("ok"))
    hint = "Crouch termico — protezione motori surriscaldati."
    if ok and plan and not shift_ok:
        hint += " Spostamento peso fallito — crouch eseguito comunque."
    elif not ok:
        hint = "Crouch termico fallito — vedi log Sport."
    return {
        "ok": ok,
        "mode": "thermal_crouch",
        "steps": steps,
        "weight_plan": plan,
        "weight_shift_ok": shift_ok,
        "hint": hint,
    }


def invoke_thermal_return_to_stand(
    *,
    clear_threshold_c: int,
    balance_threshold_c: int,
    hysteresis_c: int,
    trigger: str = "auto-termico",
) -> dict[str, Any]:
    """Stand up normale — uscita da autobilanciamento (soglia bilancio − isteresi)."""
    blocked = _gate_sport_or_block("thermal_return_stand", trigger=trigger)
    if blocked:
        return blocked
    stand = invoke_sport_pose("stand_up", trigger=trigger)
    log_return_to_stand_from_balance(
        stand,
        trigger=trigger,
        clear_threshold_c=clear_threshold_c,
        balance_threshold_c=balance_threshold_c,
        hysteresis_c=hysteresis_c,
    )
    return {
        "ok": stand.get("ok"),
        "mode": "thermal_return_stand",
        "steps": {"stand_up": stand},
        "clear_threshold_c": clear_threshold_c,
        "balance_threshold_c": balance_threshold_c,
        "threshold_hysteresis_c": hysteresis_c,
        "hint": (
            f"Ritorno stand up — motori sotto {clear_threshold_c}°C "
            f"(bilancio {balance_threshold_c}°C − isteresi {hysteresis_c}°C)."
        ),
    }

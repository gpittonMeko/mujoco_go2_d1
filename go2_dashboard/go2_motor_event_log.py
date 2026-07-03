"""Log eventi movimenti / termica Go2 per la UI motor health."""

from __future__ import annotations

import threading
import time
from typing import Any

_MAX = int(__import__("os").environ.get("GO2_MOTOR_EVENT_LOG_MAX", "150"))
_LOCK = threading.Lock()
_EVENTS: list[dict[str, Any]] = []


def log_motor_event(
    category: str,
    message: str,
    *,
    level: str = "info",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "epoch": time.time(),
        "level": level,
        "category": category,
        "message": message,
        "detail": detail or {},
    }
    with _LOCK:
        _EVENTS.append(entry)
        if len(_EVENTS) > _MAX:
            del _EVENTS[: len(_EVENTS) - _MAX]
    return entry


def get_motor_events(*, limit: int = 80) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), _MAX))
    with _LOCK:
        return list(_EVENTS[-lim:])


def format_motor_list(motors: list[dict[str, Any]]) -> str:
    if not motors:
        return "—"
    return ", ".join(f"{m.get('name')} {m.get('temperature_c')}°C" for m in motors)


def format_sport_steps(result: dict[str, Any]) -> str:
    """Riassunto codici RPC per log UI."""
    parts: list[str] = []
    for block_name in ("motion_prepare", "steps"):
        block = result.get(block_name)
        if not isinstance(block, dict):
            continue
        for step, info in block.items():
            if step.endswith("_s") or step in {"motion_select_settle_s"}:
                continue
            if not isinstance(info, dict):
                continue
            if "code" in info:
                code = info.get("code")
                meaning = info.get("meaning") or ""
                short = meaning.split("—")[0].strip() if meaning else f"code={code}"
                parts.append(f"{step}={code} ({short})")
            elif step == "motion_check_mode" and info.get("current_mode"):
                parts.append(f"mode_attuale={info.get('current_mode')}")
            elif step == "motion_skip_select":
                parts.append(f"skip_select={info.get('reason')}")
    return "; ".join(parts) if parts else ""


def _append_sport_error(msg: str, result: dict[str, Any]) -> str:
    detail = format_sport_steps(result)
    if detail:
        msg += f" — {detail}"
    elif result.get("reason") == "bad_json":
        tail = result.get("stderr_tail") or result.get("stdout_tail") or result.get("stdout")
        if tail:
            msg += f" — output subprocess: {str(tail)[:300]}"
        else:
            msg += " — subprocess senza JSON valido"
    elif result.get("reason") == "sport_service_switch_failed":
        sw = result.get("sport_service_switch") if isinstance(result.get("sport_service_switch"), dict) else {}
        code = sw.get("service_switch_code")
        hint = sw.get("hint_it") or sw.get("hint")
        if code is not None:
            msg += f" — ServiceSwitch sport_mode code {code}"
        if hint:
            msg += f" — {hint}"
        probe = result.get("sport_probe")
        if isinstance(probe, dict) and probe.get("sport_api_version_code") is not None:
            msg += f" — Sport API code {probe.get('sport_api_version_code')}"
    elif result.get("hint"):
        msg += f" — {result.get('hint')}"
    elif result.get("reason"):
        msg += f" — {result.get('reason')}"
    if result.get("stderr_tail") and result.get("reason") != "bad_json":
        msg += f" | stderr: {str(result['stderr_tail'])[:180]}"
    return msg


def log_weight_shift(plan: dict[str, Any], result: dict[str, Any], *, trigger: str) -> None:
    reasons = ", ".join(plan.get("reasons") or [])
    ok = result.get("ok")
    msg = (
        f"[{trigger}] Spostamento peso vx={plan.get('vx')} vy={plan.get('vy')} "
        f"durata={plan.get('duration_s')}s — {reasons or 'riequilibrio'}"
    )
    if not ok:
        msg = _append_sport_error(msg + " — ERRORE", result)
    log_motor_event("move", msg, level="warn" if ok else "critical", detail={"plan": plan, "result": result})


def log_balance_stand(result: dict[str, Any], *, trigger: str) -> None:
    ok = result.get("ok")
    msg = f"[{trigger}] BalanceStand — stabilizzazione posture"
    if not ok:
        msg = _append_sport_error(msg + " — ERRORE", result)
    log_motor_event("balance", msg, level="info" if ok else "critical", detail=result)


def log_crouch(motors: list[dict[str, Any]], result: dict[str, Any], *, trigger: str) -> None:
    ok = result.get("ok")
    msg = f"[{trigger}] Crouch automatico — motori: {format_motor_list(motors)}"
    if not ok:
        msg = _append_sport_error(msg + " — ERRORE", result)
    log_motor_event("crouch", msg, level="critical", detail={"motors": motors, "result": result})


def log_manual_pose(mode: str, result: dict[str, Any]) -> None:
    label = "Stand up" if mode == "stand_up" else "Crouch"
    ok = result.get("ok")
    msg = f"[manuale] {label}"
    if not ok:
        msg = _append_sport_error(msg + " — ERRORE", result)
    log_motor_event("manual", msg, level="info" if ok else "critical", detail=result)


def log_crouch_recovery_stand_up(result: dict[str, Any], *, trigger: str) -> None:
    ok = result.get("ok")
    msg = f"[{trigger}] Stand up — uscita da crouch prima dell'autobilanciamento"
    if not ok:
        msg = _append_sport_error(msg + " — ERRORE", result)
    log_motor_event("recovery", msg, level="info" if ok else "critical", detail=result)


def log_return_to_stand_from_balance(
    result: dict[str, Any],
    *,
    trigger: str,
    clear_threshold_c: int,
    balance_threshold_c: int,
    hysteresis_c: int,
) -> None:
    ok = result.get("ok")
    msg = (
        f"[{trigger}] Ritorno stand up — temp max ≤ {clear_threshold_c}°C "
        f"(bilancio {balance_threshold_c}°C − isteresi {hysteresis_c}°C)"
    )
    if not ok:
        msg = _append_sport_error(msg + " — ERRORE", result)
    log_motor_event("recovery", msg, level="info" if ok else "critical", detail=result)


def log_crouch_to_balance_recovery(
    result: dict[str, Any],
    *,
    trigger: str,
    recovery_threshold_c: int,
    crouch_threshold_c: int,
    hysteresis_c: int,
    max_temp_c: int | None = None,
    max_motor: str | None = None,
) -> None:
    ok = result.get("ok")
    temp_part = ""
    if max_temp_c is not None:
        temp_part = f"temp max {max_temp_c}°C"
        if max_motor:
            temp_part += f" ({max_motor})"
        temp_part += " ≤ "
    msg = (
        f"[{trigger}] Recupero crouch → autobilanciamento — {temp_part}"
        f"{recovery_threshold_c}°C (crouch {crouch_threshold_c}°C − isteresi {hysteresis_c}°C)"
    )
    steps = result.get("steps") if isinstance(result.get("steps"), dict) else {}
    stand = steps.get("stand_up") if isinstance(steps.get("stand_up"), dict) else {}
    if stand and not stand.get("ok"):
        ok = False
    if ok:
        msg += " — OK"
        step_detail = format_sport_steps(result)
        if step_detail:
            msg += f" ({step_detail})"
    else:
        msg = _append_sport_error(msg + " — ERRORE", result)
    log_motor_event("balance", msg, level="info" if ok else "critical", detail=result)


def log_thermal_watch(
    level: str,
    motors: list[dict[str, Any]],
    threshold_c: int,
    *,
    kind: str = "balance",
    max_temp_c: int | None = None,
    max_motor: str | None = None,
) -> None:
    extra = ""
    if max_temp_c is not None:
        extra = f" · temp max {max_temp_c}°C"
        if max_motor:
            extra += f" ({max_motor})"
    label = "Soglia crouch" if kind == "crouch" else "Soglia bilancio"
    log_motor_event(
        "thermal",
        f"{label} {threshold_c}°C — {format_motor_list(motors)}{extra}",
        level=level,
        detail={
            "kind": kind,
            "threshold_c": threshold_c,
            "motors": motors,
            "max_temp_c": max_temp_c,
            "max_motor": max_motor,
        },
    )

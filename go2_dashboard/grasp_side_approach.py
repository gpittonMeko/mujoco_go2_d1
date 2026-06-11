"""Flusso «presa di lato» (side-approach): evita di avere la testa del cane in mezzo alle scatole.

Idea operatore: invece di prendere l'oggetto frontale-frontale (dove testa/camera del cane
sono tra pinza e oggetto), si **gira il cane di 90° a destra** e si porta il **braccio al preset
START laterale** (``start_alignment_lateral.json``, default operativo). Così il braccio raggiunge
le scatole **di lato**, con la testa fuori dalla traiettoria.

Sequenza:
  1. ``front_detect``  — la camera frontale (logical 6) conferma che l'oggetto è inquadrato.
  2. ``posture``       — se accovacciato → ``stand_up``; se già in piedi → due passi avanti.
  3. ``turn_right``    — rotazione in place ~90° a destra (yaw-rate * durata).
  4. ``arm_start_lateral`` — braccio al preset START laterale salvato.

**Esecuzione in background**: con ``confirm`` il flusso parte in un thread separato e l'endpoint
risponde subito (202). La UI segue l'avanzamento via ``GET /api/grasp/side_approach_status``. Così
la dashboard non resta bloccata mentre il cane si alza / cammina / gira (stand_up può prendere
secondi). I comandi base usano la via **async** di ``accompany_execute_json`` (come il tab Movimenti)
e il ritmo è dato da sleep lato thread (non blocca Flask, che gira ``threaded=True``).

I gate hardware (``GO2_LOCAL`` / ``GO2_ENABLE_BASE_MOTION`` / ``GO2_ENABLE_REAL_ARM`` / flag
plan-execute) restano quelli dei mattoni sottostanti: questo modulo non li bypassa. Il braccio gira
tramite ``operator_arm_motion`` (sessione live persistente, come «Braccio D1 · giunti») → niente kill
del publisher DDS, niente cedimento (vedi regola funcode-hold).
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT

CONFIRM_TOKEN = "RUN_SIDE_GRASP_SETUP"

_JOB_LOCK = threading.Lock()
_JOB: dict[str, Any] = {
    "running": False,
    "flow": "side_approach_setup",
    "ok": None,
    "started_at": None,
    "finished_at": None,
    "current_step": None,
    "failed_step": None,
    "label_it": "Nessun setup avviato.",
    "steps": [],
    "params": None,
    "posture_effective": None,
}


def _truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _job_snapshot() -> dict[str, Any]:
    with _JOB_LOCK:
        snap = json.loads(json.dumps({k: v for k, v in _JOB.items()}, default=str))
    snap["ok"] = snap.get("ok")
    return snap


def side_approach_status() -> dict[str, Any]:
    """Stato corrente del job side-approach (per polling UI)."""
    return _job_snapshot()


def _job_set(**kw: Any) -> None:
    with _JOB_LOCK:
        _JOB.update(kw)


def _job_step(step: dict[str, Any]) -> None:
    with _JOB_LOCK:
        _JOB["steps"].append(step)
        _JOB["current_step"] = step.get("step")


# --------------------------------------------------------------------------------------
# Primitive base (Sport DDS) — via async, come il tab Movimenti
# --------------------------------------------------------------------------------------
def _base_async(payload: dict[str, Any]) -> dict[str, Any]:
    """Invia un comando Sport **non bloccante** (async/202) e ritorna esito sintetico."""
    from go2_dashboard.sport_lane import accompany_execute_json

    body = {"sync": False, **payload}
    res, code = accompany_execute_json(body, query_sync_flag=False)
    # async ⇒ 202 con accepted:true; sync fallback ⇒ 200/ok
    ok = bool(code < 400 and isinstance(res, dict) and (res.get("accepted") or res.get("ok", code < 400)))
    return {"ok": ok, "http_status": code, "mode": payload.get("mode"), "result": res}


def _hold_velocity(*, vx: float, vy: float, vyaw: float, duration_s: float) -> dict[str, Any]:
    """Mantiene una velocità Sport per ``duration_s`` (re-invio periodico async) poi stop."""
    from go2_dashboard.sport_lane import accompany_execute_json

    t_end = time.time() + max(0.0, duration_s)
    ticks = 0
    first_code: int | None = None
    while time.time() < t_end:
        res, code = accompany_execute_json(
            {"mode": "velocity", "vx": vx, "vy": vy, "vyaw": vyaw, "pre_balance": ticks == 0, "sync": False},
            query_sync_flag=False,
        )
        if first_code is None:
            first_code = code
        ticks += 1
        time.sleep(0.2)
    _stop_res, stop_code = accompany_execute_json({"mode": "stop", "sync": False}, query_sync_flag=False)
    return {
        "ok": bool((first_code or 500) < 400 and stop_code < 400),
        "ticks": ticks,
        "duration_s": round(duration_s, 2),
        "vx": vx,
        "vy": vy,
        "vyaw": vyaw,
        "stop_http_status": stop_code,
    }


def _resolve_posture(posture: str) -> str:
    """``auto`` → deduce da ultimo comando Sport (stand_down/crouch ⇒ crouch), altrimenti standing."""
    p = (posture or "auto").strip().lower()
    if p in {"crouch", "standing"}:
        return p
    try:
        from go2_dashboard.sport_lane import sport_last_payload

        last_mode = str((sport_last_payload() or {}).get("mode") or "").lower()
        if last_mode in {"crouch", "stand_down", "standdown", "sit", "damping", "damp"}:
            return "crouch"
    except Exception:
        pass
    return "standing"


# --------------------------------------------------------------------------------------
# Braccio: START laterale (preset dedicato, default operativo)
# --------------------------------------------------------------------------------------
def _arm_start_lateral() -> dict[str, Any]:
    """Porta il braccio al preset START laterale (``start_alignment_lateral.json``)."""
    from go2_dashboard import d1_arm_motion
    from go2_dashboard.d1_arm_publish_lite import START_VARIANT_LATERAL, goto_saved_start_from_json

    beg = d1_arm_motion.ensure_grasp_motion_worker()
    if not (beg.get("ok") or beg.get("skipped")):
        return {"step": "arm_start_lateral", "ok": False, "reason": "arm_session_failed", "motion_worker": beg}

    mv = goto_saved_start_from_json(start_variant=START_VARIANT_LATERAL)
    return {
        "step": "arm_start_lateral",
        "ok": bool(mv.get("ok")),
        "start_variant": START_VARIANT_LATERAL,
        "motion": mv,
        "label_it": "Braccio a START laterale (preset standard)",
        **({k: mv[k] for k in ("reason", "hint_it", "start_alignment_file") if k in mv}),
    }


# --------------------------------------------------------------------------------------
# Parametri + detection
# --------------------------------------------------------------------------------------
def _build_params() -> dict[str, Any]:
    walk_vx = _envf("GO2_SIDE_WALK_VX", 0.30)
    walk_s = _envf("GO2_SIDE_WALK_S", 1.2)
    turn_vyaw = abs(_envf("GO2_SIDE_TURN_VYAW", 0.45))
    turn_deg = _envf("GO2_SIDE_TURN_DEG", 90.0)
    turn_dir = -1.0 if _truthy("GO2_SIDE_TURN_RIGHT", "1") else 1.0
    turn_s = (math.radians(turn_deg) / turn_vyaw) if turn_vyaw > 1e-3 else 0.0
    return {
        "walk_vx": walk_vx,
        "walk_s": walk_s,
        "turn_vyaw": turn_vyaw,
        "turn_deg": turn_deg,
        "turn_dir": turn_dir,
        "turn_s": round(turn_s, 2),
        "stand_wait_s": _envf("GO2_SIDE_STAND_WAIT_S", 4.5),
        "settle_s": _envf("GO2_SIDE_SETTLE_S", 0.8),
        "start_variant": "lateral",
    }


def _front_detect(front_camera: int) -> dict[str, Any]:
    try:
        from go2_dashboard.grasp_full_sequence import _detect_on_camera

        det = _detect_on_camera(int(front_camera))
        return {"step": "front_detect", **det}
    except Exception as exc:  # noqa: BLE001
        return {"step": "front_detect", "ok": False, "reason": "front_detect_error", "detail": repr(exc)}


# --------------------------------------------------------------------------------------
# Esecuzione (background thread)
# --------------------------------------------------------------------------------------
def _execute_sequence(*, posture: str, front_camera: int, params: dict[str, Any]) -> None:
    from go2_dashboard.operator_stack import go2_local

    settle = float(params["settle_s"])
    try:
        # 1) detect frontale (informativa)
        _job_step(_front_detect(front_camera))

        if not go2_local():
            _job_set(ok=False, failed_step="env", label_it="GO2_LOCAL non attivo: niente movimento sull'hardware.")
            return

        # 2) postura
        posture_eff = _resolve_posture(posture)
        _job_set(posture_effective=posture_eff)
        if posture_eff == "crouch":
            r = _base_async({"mode": "stand_up", "enable": True})
            r.update({"step": "posture", "action": "stand_up", "label_it": "Mi alzo (ero accovacciato)…"})
            _job_step(r)
            if not r.get("ok"):
                _job_set(ok=False, failed_step="posture", label_it="Stand_up rifiutato dal Sport DDS.")
                return
            time.sleep(float(params["stand_wait_s"]))
        else:
            r = _hold_velocity(vx=float(params["walk_vx"]), vy=0.0, vyaw=0.0, duration_s=float(params["walk_s"]))
            r.update({"step": "posture", "action": "walk_forward", "label_it": "Due passi avanti (ero in piedi)…"})
            _job_step(r)
            if not r.get("ok"):
                _job_set(ok=False, failed_step="posture", label_it="Comando velocità (passi avanti) rifiutato.")
                return
            time.sleep(settle)

        # 3) rotazione ~90° a destra
        r = _hold_velocity(
            vx=0.0, vy=0.0, vyaw=float(params["turn_dir"]) * float(params["turn_vyaw"]), duration_s=float(params["turn_s"])
        )
        r.update({"step": "turn_right", "action": "turn", "label_it": f"Giro {params['turn_deg']:.0f}° a destra…"})
        _job_step(r)
        if not r.get("ok"):
            _job_set(ok=False, failed_step="turn_right", label_it="Comando rotazione rifiutato.")
            return
        time.sleep(settle)

        # 4) braccio a START laterale (preset standard)
        r = _arm_start_lateral()
        _job_step(r)
        if not r.get("ok"):
            _job_set(ok=False, failed_step="arm_start_lateral", label_it=f"Braccio: {r.get('reason', 'errore')}.")
            return

        _job_set(ok=True, label_it="Setup presa di lato completato: cane girato, braccio a START laterale. Ora usa il coach.")
    except Exception as exc:  # noqa: BLE001
        _job_set(ok=False, failed_step=_JOB.get("current_step") or "exception", label_it=f"Errore: {exc!r}")
    finally:
        _job_set(running=False, finished_at=_now_iso(), current_step=None)


def start_side_approach_setup(
    *,
    instruction: str = "",
    confirm: str | None = None,
    posture: str = "auto",
    front_camera: int = 6,
) -> tuple[dict[str, Any], int]:
    """Avvia il flusso side-approach. Ritorna ``(payload, http_status)``.

    - Senza ``confirm``: dry-run sincrono (solo detection frontale + piano), 200.
    - Con ``confirm``: avvia il job in background e risponde 202 (la UI polla ``/status``).
    - Se un job è già in corso: 409.
    """
    params = _build_params()
    move_allowed = confirm == CONFIRM_TOKEN

    if not move_allowed:
        # dry-run veloce: detection + piano, nessun movimento
        det = _front_detect(front_camera)
        posture_eff = _resolve_posture(posture)
        out = {
            "ok": True,
            "flow": "side_approach_setup",
            "started": False,
            "move_allowed": False,
            "instruction": instruction,
            "posture_effective": posture_eff,
            "params": params,
            "steps": [
                det,
                {"step": "posture", "skipped": True, "reason": "confirm_required", "posture": posture_eff},
                {"step": "turn_right", "skipped": True, "reason": "confirm_required"},
                {"step": "arm_start_lateral", "skipped": True, "reason": "confirm_required"},
            ],
            "label_it": "Dry-run: detection + piano movimenti (manda confirm=RUN_SIDE_GRASP_SETUP per eseguire).",
        }
        return out, 200

    with _JOB_LOCK:
        if _JOB.get("running"):
            return (
                {
                    "ok": False,
                    "started": False,
                    "reason": "already_running",
                    "label_it": "Un setup presa di lato è già in corso. Attendi che finisca.",
                    "current_step": _JOB.get("current_step"),
                },
                409,
            )
        _JOB.update(
            {
                "running": True,
                "ok": None,
                "started_at": _now_iso(),
                "finished_at": None,
                "current_step": "front_detect",
                "failed_step": None,
                "label_it": "Setup presa di lato avviato…",
                "steps": [],
                "params": params,
                "posture_effective": None,
                "instruction": instruction,
            }
        )

    threading.Thread(
        target=_execute_sequence,
        kwargs={"posture": posture, "front_camera": int(front_camera), "params": params},
        name="side-approach-setup",
        daemon=True,
    ).start()

    return (
        {
            "ok": True,
            "started": True,
            "flow": "side_approach_setup",
            "move_allowed": True,
            "params": params,
            "label_it": "Setup presa di lato avviato. Segui l'avanzamento qui sotto.",
            "poll": "/api/grasp/side_approach_status",
        },
        202,
    )


# Compat: vecchio nome sincrono (dry-run inline). Mantiene l'API import-stabile.
def run_side_approach_setup(
    *,
    instruction: str = "",
    confirm: str | None = None,
    posture: str = "auto",
    front_camera: int = 6,
) -> dict[str, Any]:
    out, _code = start_side_approach_setup(
        instruction=instruction, confirm=confirm, posture=posture, front_camera=front_camera
    )
    return out

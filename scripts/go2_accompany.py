#!/usr/bin/env python3
"""
Sport API (Go2): pose base — StandUp / StandDown (crouch), oppure joystick RC / Damp (uso avanzato).

Richiede ``unitree_sdk2py`` e Cyclone DDS Python sullo stesso host del robot.
La dashboard chiama questo modulo solo con GO2_ENABLE_BASE_MOTION=1 e GO2_LOCAL=1.
Opzionale: ``UNITREE_SDK2_PYTHON`` = path assoluto alla cartella che contiene ``unitree_sdk2py/`` (un solo SDK).

Note da risorse online / SDK:
- Il controllo high-level richiede il servizio **sport_mode** attivo sul robot; in molti casi va abilitato
  dall'app ufficiale Unitree Go2 (vedi discussion su ``unitree_sdk2_python`` issue #19).
- Se un'altra modalità motion tiene il robot (low-level / altro stack), gli esempi ufficiali usano
  ``MotionSwitcherClient`` (CheckMode / ReleaseMode / SelectMode) prima dei comandi Sport.
  Opzionale qui: ``GO2_SPORT_MOTION_PREPARE=1`` e variabili correlate sotto.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_factory_initialized = False
_sport_client = None


def _ensure_cyclone_factory(domain: int, iface: str | None) -> None:
    """Un solo ``ChannelFactoryInitialize`` per processo — doppia init Cyclone può abortire l'interprete."""
    global _factory_initialized
    if _factory_initialized:
        return
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize

    if iface:
        ChannelFactoryInitialize(domain, iface)
    else:
        ChannelFactoryInitialize(domain)
    _factory_initialized = True


def ensure_go2_dds_channel_factory_from_env(project_root: Path | None = None) -> None:
    """Inizializza Cyclone DDS una sola volta nel processo usando ``GO2_DDS_*`` (Sport / Voice RPC)."""
    root = project_root if project_root is not None else Path(__file__).resolve().parent.parent
    _ensure_sdk_path(root)
    domain = int(os.environ.get("GO2_DDS_DOMAIN", "0"))
    iface_raw = os.environ.get("GO2_DDS_INTERFACE", "").strip()
    iface = iface_raw if iface_raw else None
    with _lock:
        _ensure_cyclone_factory(domain, iface)


def _sport_rpc_code_explain(code: int) -> str:
    """Decodifica codici unitree_sdk2py.rpc.internal (0 = OK)."""
    try:
        from unitree_sdk2py.rpc.internal import (
            RPC_ERR_CLIENT_API_DATA,
            RPC_ERR_CLIENT_API_NOT_MATCH,
            RPC_ERR_CLIENT_API_NOT_REG,
            RPC_ERR_CLIENT_API_TIMEOUT,
            RPC_ERR_CLIENT_LEASE_INVALID,
            RPC_ERR_CLIENT_SEND,
            RPC_ERR_SERVER_API_NOT_IMPL,
            RPC_ERR_SERVER_API_PARAMETER,
            RPC_ERR_SERVER_INTERNAL,
            RPC_ERR_SERVER_LEASE_DENIED,
            RPC_ERR_SERVER_LEASE_EXIST,
            RPC_ERR_SERVER_LEASE_NOT_EXIST,
            RPC_ERR_SERVER_SEND,
            RPC_ERR_UNKNOWN,
            RPC_OK,
        )
    except Exception:
        if code == 0:
            return "OK"
        if code == 3102:
            return (
                "RPC_ERR_CLIENT_SEND (3102): DDS non ha accettato/inviato la richiesta — "
                "controlla GO2_DDS_INTERFACE (es. eth0 sulla Jetson verso il robot), "
                "domain Cyclone uguale al robot, robot acceso e servizio sport attivo."
            )
        return f"codice {code} (import internal fallito)"

    table: dict[int, str] = {
        RPC_OK: "OK (0) — risposta Sport/RPC senza errore lato trasporto.",
        RPC_ERR_UNKNOWN: "RPC_ERR_UNKNOWN (3001)",
        RPC_ERR_CLIENT_SEND: (
            "RPC_ERR_CLIENT_SEND (3102): SendRequest DDS fallito (future=None) — "
            "interfaccia di rete errata o assente, domain DDS errato, nessun peer sport, o stack DDS non inizializzato."
        ),
        RPC_ERR_CLIENT_API_NOT_REG: "RPC_ERR_CLIENT_API_NOT_REG (3103): API Sport non registrata sul client.",
        RPC_ERR_CLIENT_API_TIMEOUT: "RPC_ERR_CLIENT_API_TIMEOUT (3104): timeout in attesa della risposta Sport.",
        RPC_ERR_CLIENT_API_NOT_MATCH: "RPC_ERR_CLIENT_API_NOT_MATCH (3105): mismatch api_id nella risposta.",
        RPC_ERR_CLIENT_API_DATA: "RPC_ERR_CLIENT_API_DATA (3106)",
        RPC_ERR_CLIENT_LEASE_INVALID: "RPC_ERR_CLIENT_LEASE_INVALID (3107)",
        RPC_ERR_SERVER_SEND: "RPC_ERR_SERVER_SEND (3201)",
        RPC_ERR_SERVER_INTERNAL: "RPC_ERR_SERVER_INTERNAL (3202)",
        RPC_ERR_SERVER_API_NOT_IMPL: "RPC_ERR_SERVER_API_NOT_IMPL (3203)",
        RPC_ERR_SERVER_API_PARAMETER: "RPC_ERR_SERVER_API_PARAMETER (3204)",
        RPC_ERR_SERVER_LEASE_DENIED: "RPC_ERR_SERVER_LEASE_DENIED (3205)",
        RPC_ERR_SERVER_LEASE_NOT_EXIST: "RPC_ERR_SERVER_LEASE_NOT_EXIST (3206)",
        RPC_ERR_SERVER_LEASE_EXIST: "RPC_ERR_SERVER_LEASE_EXIST (3207)",
    }
    return table.get(code, f"codice_Sport/RPC_{code}")


def _steps_with_meanings(steps: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in steps.items():
        if isinstance(v, dict) and "code" in v:
            c = int(v["code"])
            entry = dict(v)
            entry["meaning"] = _sport_rpc_code_explain(c)
            out[k] = entry
        else:
            out[k] = v
    return out


def _sport_steps_all_ok(steps: dict[str, Any]) -> bool:
    for v in steps.values():
        if isinstance(v, dict) and "code" in v:
            if int(v["code"]) != 0:
                return False
    return True


def _sport_steps_ok_allow_benign_stop_move(steps: dict[str, Any]) -> bool:
    """StopMove può restituire -1 se il cane è già fermo — non invalidare StandDown/StandUp riusciti."""
    sm = steps.get("stop_move")
    sm_code = int(sm.get("code", 0)) if isinstance(sm, dict) else 0
    for key, val in steps.items():
        if key == "stop_move":
            continue
        if isinstance(val, dict) and "code" in val and int(val["code"]) != 0:
            return False
    if sm_code in {0, -1}:
        return True
    return False


def _sport_motion_prepare_steps() -> dict[str, Any]:
    """
    MotionSwitcher opzionale prima di StandUp/StandDown (pattern da ``go2_stand_example.py`` / motion_switcher_example).

    Env:
    - ``GO2_SPORT_MOTION_PREPARE``: ``1`` per eseguire CheckMode e passi successivi.
    - ``GO2_SPORT_RELEASE_IF_HELD`` (default ``1``): se CheckMode ha ``name`` non vuoto, chiama ReleaseMode().
    - ``GO2_SPORT_SELECT_MODE``: se non vuoto (es. ``normal``, ``ai``), chiama SelectMode(name).
    """
    if os.environ.get("GO2_SPORT_MOTION_PREPARE", "0").lower() not in {"1", "true", "yes"}:
        return {}
    try:
        from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient
    except Exception as exc:
        return {"motion_prepare_error": {"reason": repr(exc)}}

    steps: dict[str, Any] = {}
    msc = MotionSwitcherClient()
    msc.SetTimeout(5.0)
    msc.Init()
    code, result = msc.CheckMode()
    steps["motion_check_mode"] = {"code": code, "result": result}
    release = os.environ.get("GO2_SPORT_RELEASE_IF_HELD", "1").lower() in {"1", "true", "yes"}
    if release and result and isinstance(result, dict) and result.get("name"):
        code_rm, _ = msc.ReleaseMode()
        steps["motion_release_mode"] = {"code": code_rm}
    sel = (os.environ.get("GO2_SPORT_SELECT_MODE") or "").strip()
    if sel:
        code_sm, _ = msc.SelectMode(sel)
        steps["motion_select_mode"] = {"code": code_sm, "name": sel}
    return steps


def _ensure_sdk_path(project_root: Path) -> None:
    """Mette in cima a ``sys.path`` la prima directory valida che contiene ``unitree_sdk2py``."""
    raw = (os.environ.get("UNITREE_SDK2_PYTHON") or "").strip()
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw).expanduser())
    candidates.append(project_root / "unitree_sdk2_python")
    for p in candidates:
        try:
            if p.is_dir() and (p / "unitree_sdk2py").is_dir():
                s = str(p.resolve())
                if s not in sys.path:
                    sys.path.insert(0, s)
                return
        except OSError:
            continue
    s = str(project_root / "unitree_sdk2_python")
    if s not in sys.path:
        sys.path.insert(0, s)


def dds_unitree_motion_ping(
    *,
    project_root: Path,
    domain: int,
    iface: str | None,
) -> dict[str, Any]:
    """
    Verifica DDS verso il cane senza muovere le zampe: ``MotionSwitcherClient.CheckMode``.

    Stesso ``ChannelFactory`` / dominio / interfaccia usati da ``SportClient`` — se questo fallisce (es. 3102),
    anche i comandi Sport falliscono allo stesso modo.
    """
    _ensure_sdk_path(project_root)
    try:
        from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient
    except Exception as exc:
        return {"ok": False, "reason": f"sdk_import_failed: {exc!r}"}
    try:
        with _lock:
            _ensure_cyclone_factory(domain, iface)
            msc = MotionSwitcherClient()
            msc.SetTimeout(8.0)
            msc.Init()
            code, result = msc.CheckMode()
        ok = code == 0
        return {
            "ok": ok,
            "motion_switcher_check_code": code,
            "motion_switcher_check_meaning": None if code == 0 else _sport_rpc_code_explain(code),
            "motion_mode_result": result,
            "dds_domain": domain,
            "dds_interface": iface,
            "hint_it": (
                "MotionSwitcher CheckMode OK — DDS raggiunge il cane (stesso bus di SportClient)."
                if ok
                else "CheckMode fallito: GO2_DDS_INTERFACE / GO2_DDS_DOMAIN, cane acceso, stessa LAN L2."
            ),
        }
    except Exception as exc:
        return {"ok": False, "reason": repr(exc), "dds_domain": domain, "dds_interface": iface}


def sport_accompany(
    *,
    project_root: Path,
    domain: int,
    iface: str | None,
    enable: bool,
    mode: str = "joystick",
    stand_up_first: bool = False,
    speed_level: int | None = None,
    vx: float | None = None,
    vy: float | None = None,
    vyaw: float | None = None,
    pre_balance: bool = True,
) -> dict[str, Any]:
    """
    mode ``stand_up``: ``StopMove`` → ``StandUp()`` → ``BalanceStand()`` — base in piedi.

    mode ``crouch``: ``StopMove`` → ``StandDown()`` — abbassamento (accucciato / passiva secondo firmware).

    mode ``joystick``: BalanceStand + SpeedLevel + SwitchJoystick — comando da telecomando RC.

    mode ``damping``: ``Damp()`` — giunti smorzati (area libera). ``enable=false``: RecoveryStand + BalanceStand.

    mode ``balance_hold``: solo BalanceStand — equilibrio senza joystick.

    mode ``stop``: solo ``StopMove``.

    mode ``recovery_stand``: ``RecoveryStand`` → ``BalanceStand`` (ripresa da situazioni sporche).

    mode ``velocity``: ``Move(vx, vy, vyaw)`` — SDK Sport (m/s e rad/s come da firmware).
      Richiede i parametri ``vx``, ``vy``, ``vyaw`` oppure vengono usati 0.
      Opzionale ``pre_balance`` (default ``True``): chiama ``BalanceStand`` prima del Move.

    Per ``stand_up`` / ``crouch`` il flag ``enable`` è ignorato.
    """
    global _sport_client
    _ensure_sdk_path(project_root)
    try:
        from unitree_sdk2py.go2.sport.sport_client import SportClient
    except Exception as exc:
        return {"ok": False, "reason": f"sdk_import_failed: {exc!r}"}

    if mode not in {
        "joystick",
        "damping",
        "balance_hold",
        "stand_up",
        "crouch",
        "stop",
        "recovery_stand",
        "velocity",
    }:
        return {
            "ok": False,
            "reason": (
                f"unknown mode {mode!r}; use stand_up|crouch|stop|recovery_stand|velocity|"
                "joystick|damping|balance_hold"
            ),
        }

    lvl = speed_level
    if lvl is None:
        lvl = int(os.environ.get("GO2_ACCOMPANY_SPEED_LEVEL", "1"))

    steps: dict[str, Any] = {}

    with _lock:
        try:
            _ensure_cyclone_factory(domain, iface)

            if _sport_client is None:
                _sport_client = SportClient()
                _sport_client.SetTimeout(10.0)
                _sport_client.Init()

            sc = _sport_client

            motion_pre: dict[str, Any] = {}
            if mode in {"crouch", "stand_up"}:
                motion_pre = _sport_motion_prepare_steps()

            if mode == "stand_up":
                code = sc.StopMove()
                steps["stop_move"] = {"code": code}
                code = sc.StandUp()
                steps["stand_up"] = {"code": code}
                code = sc.BalanceStand()
                steps["balance_stand"] = {"code": code}
                steps = _steps_with_meanings(steps)
                ok = _sport_steps_ok_allow_benign_stop_move(steps)
                out: dict[str, Any] = {
                    "ok": ok,
                    "mode": "stand_up",
                    "robot": "go2_quadrupede",
                    "steps": steps,
                    "hint": (
                        "StandUp + BalanceStand — corpo del cane in piedi. Non comanda il braccio D1."
                        if ok
                        else "Uno o più passi Sport hanno restituito code != 0: il comando potrebbe non essere stato eseguito."
                    ),
                }
                if motion_pre:
                    out["motion_prepare"] = motion_pre
                return out

            if mode == "crouch":
                code = sc.StopMove()
                steps["stop_move"] = {"code": code}
                code = sc.StandDown()
                steps["stand_down"] = {"code": code}
                steps = _steps_with_meanings(steps)
                ok = _sport_steps_ok_allow_benign_stop_move(steps)
                out_c: dict[str, Any] = {
                    "ok": ok,
                    "mode": "crouch",
                    "robot": "go2_quadrupede",
                    "steps": steps,
                    "hint": (
                        "StandDown — corpo del cane abbassato (accucciato). Non comanda il braccio D1."
                        if ok
                        else "StandDown/StopMove non riusciti (vedi meaning su ogni step): spesso 3102 = DDS non invia al robot."
                    ),
                }
                if motion_pre:
                    out_c["motion_prepare"] = motion_pre
                return out_c

            if mode == "stop":
                code = sc.StopMove()
                steps["stop_move"] = {"code": code}
                steps = _steps_with_meanings(steps)
                ok = _sport_steps_all_ok(steps)
                return {
                    "ok": ok,
                    "mode": "stop",
                    "robot": "go2_quadrupede",
                    "steps": steps,
                    "hint": "StopMove — ferma il movimento di marcia richiesto dalla Sport API.",
                }

            if mode == "recovery_stand":
                code = sc.RecoveryStand()
                steps["recovery_stand"] = {"code": code}
                code = sc.BalanceStand()
                steps["balance_stand"] = {"code": code}
                steps = _steps_with_meanings(steps)
                ok = _sport_steps_all_ok(steps)
                return {
                    "ok": ok,
                    "mode": "recovery_stand",
                    "robot": "go2_quadrupede",
                    "steps": steps,
                    "hint": (
                        "RecoveryStand + BalanceStand — prova a rialzare/ristabilizzare la base."
                        if ok
                        else "Uno o più passi Sport falliti — vedi meaning nei codici."
                    ),
                }

            if mode == "velocity":
                try:
                    vx_f = float(vx) if vx is not None else 0.0
                    vy_f = float(vy) if vy is not None else 0.0
                    vyaw_f = float(vyaw) if vyaw is not None else 0.0
                except (TypeError, ValueError):
                    return {"ok": False, "reason": "vx_vy_vyaw_invalid", "mode": "velocity"}
                if pre_balance:
                    code = sc.BalanceStand()
                    steps["balance_stand"] = {"code": code}
                code_m = sc.Move(vx_f, vy_f, vyaw_f)
                steps["move"] = {"code": code_m, "vx": vx_f, "vy": vy_f, "vyaw": vyaw_f}
                steps = _steps_with_meanings(steps)
                ok = _sport_steps_all_ok(steps)
                return {
                    "ok": ok,
                    "mode": "velocity",
                    "robot": "go2_quadrupede",
                    "vx": vx_f,
                    "vy": vy_f,
                    "vyaw": vyaw_f,
                    "steps": steps,
                    "hint": (
                        "Move(vx, vy, vyaw) inviato (no-reply RPC). Verifica area libera e modalità sport sul cane."
                        if ok
                        else "Invio Move non accettato dal client DDS — controlla rete e sport_mode."
                    ),
                }

            if mode == "damping":
                if enable:
                    code, data = sc.StopMove()
                    steps["stop_move"] = {"code": code, "data_preview": str(data)[:120]}
                    code, data = sc.SwitchJoystick(False)
                    steps["switch_joystick_off"] = {"code": code, "data_preview": str(data)[:120]}
                    code, data = sc.Damp()
                    steps["damp"] = {"code": code, "data_preview": str(data)[:120]}
                    return {
                        "ok": True,
                        "enabled": True,
                        "mode": "damping",
                        "steps": steps,
                        "hint": (
                            "Damp: robot smorzato — puoi accompagnarlo/spostarlo A MANO solo su piano libero. "
                            "enable:false → RecoveryStand+BalanceStand."
                        ),
                    }
                code, data = sc.RecoveryStand()
                steps["recovery_stand"] = {"code": code, "data_preview": str(data)[:120]}
                code, data = sc.BalanceStand()
                steps["balance_stand"] = {"code": code, "data_preview": str(data)[:120]}
                return {
                    "ok": True,
                    "enabled": False,
                    "mode": "damping",
                    "steps": steps,
                    "hint": "Uscita da Damp: ripreso equilibrio in piedi.",
                }

            if mode == "balance_hold":
                if enable:
                    if stand_up_first:
                        code, data = sc.StandUp()
                        steps["stand_up"] = {"code": code, "data_preview": str(data)[:120]}
                    code, data = sc.BalanceStand()
                    steps["balance_stand"] = {"code": code, "data_preview": str(data)[:120]}
                    return {
                        "ok": True,
                        "enabled": True,
                        "mode": "balance_hold",
                        "steps": steps,
                        "hint": "Solo equilibrio in piedi (no joystick). Utile prima di altre azioni.",
                    }
                code, data = sc.StopMove()
                steps["stop_move"] = {"code": code, "data_preview": str(data)[:120]}
                return {
                    "ok": True,
                    "enabled": False,
                    "mode": "balance_hold",
                    "steps": steps,
                    "hint": "StopMove eseguito.",
                }

            # --- joystick (default) ---
            if enable:
                if stand_up_first:
                    code, data = sc.StandUp()
                    steps["stand_up"] = {"code": code, "data_preview": str(data)[:120]}
                code, data = sc.BalanceStand()
                steps["balance_stand"] = {"code": code, "data_preview": str(data)[:120]}
                code, data = sc.SpeedLevel(int(lvl))
                steps["speed_level"] = {"code": code, "level": int(lvl), "data_preview": str(data)[:120]}
                code, data = sc.SwitchJoystick(True)
                steps["switch_joystick_on"] = {"code": code, "data_preview": str(data)[:120]}
                return {
                    "ok": True,
                    "enabled": True,
                    "mode": "joystick",
                    "steps": steps,
                    "hint": "Joystick RC Unitree attivo; Fine accompagna → StopMove + joystick off.",
                }

            code, data = sc.StopMove()
            steps["stop_move"] = {"code": code, "data_preview": str(data)[:120]}
            code, data = sc.SwitchJoystick(False)
            steps["switch_joystick_off"] = {"code": code, "data_preview": str(data)[:120]}
            return {
                "ok": True,
                "enabled": False,
                "mode": "joystick",
                "steps": steps,
                "hint": "Joystick RC disattivato.",
            }
        except Exception as exc:
            return {"ok": False, "reason": repr(exc)}


def accompany_rc_mode(
    *,
    project_root: Path,
    domain: int,
    iface: str | None,
    enable: bool,
    stand_up_first: bool = False,
    speed_level: int | None = None,
) -> dict[str, Any]:
    """Compatibilità: equivale a sport_accompany(..., mode='joystick')."""
    return sport_accompany(
        project_root=project_root,
        domain=domain,
        iface=iface,
        enable=enable,
        mode="joystick",
        stand_up_first=stand_up_first,
        speed_level=speed_level,
    )


def _sport_client_ready(
    project_root: Path,
    domain: int,
    iface: str | None,
) -> tuple[Any | None, dict[str, Any]]:
    """Inizializza SportClient (singleton processo) — ritorna (client, err_dict)."""
    global _sport_client
    _ensure_sdk_path(project_root)
    try:
        from unitree_sdk2py.go2.sport.sport_client import SportClient
    except Exception as exc:
        return None, {"ok": False, "reason": f"sdk_import_failed: {exc!r}"}
    with _lock:
        try:
            _ensure_cyclone_factory(domain, iface)
            if _sport_client is None:
                _sport_client = SportClient()
                _sport_client.SetTimeout(10.0)
                _sport_client.Init()
            return _sport_client, {}
        except Exception as exc:
            return None, {"ok": False, "reason": repr(exc)}


def sport_move(
    *,
    project_root: Path,
    domain: int,
    iface: str | None,
    vx: float,
    vy: float,
    vyaw: float,
    duration_s: float,
    stand_first: bool = True,
) -> dict[str, Any]:
    """Move(vx,vy,vyaw) per ``duration_s`` secondi, poi StopMove + BalanceStand."""
    import time as _time

    sc, err = _sport_client_ready(project_root, domain, iface)
    if sc is None:
        return err

    duration_s = max(0.05, float(duration_s))
    steps: dict[str, Any] = {}
    with _lock:
        try:
            if stand_first:
                code = sc.BalanceStand()
                steps["balance_stand"] = {"code": code}
            code = sc.Move(float(vx), float(vy), float(vyaw))
            steps["move"] = {"code": code, "vx": vx, "vy": vy, "vyaw": vyaw, "duration_s": duration_s}
            _time.sleep(duration_s)
            code = sc.StopMove()
            steps["stop_move"] = {"code": code}
            code = sc.BalanceStand()
            steps["balance_after"] = {"code": code}
            steps = _steps_with_meanings(steps)
            ok = _sport_steps_all_ok(steps)
            return {
                "ok": ok,
                "mode": "move",
                "robot": "go2_quadrupede",
                "steps": steps,
                "vx": vx,
                "vy": vy,
                "vyaw": vyaw,
                "duration_s": duration_s,
                "hint": (
                    f"Move {duration_s:.2f}s vx={vx} vy={vy} vyaw={vyaw} — poi StopMove."
                    if ok
                    else "Move/StopMove non riuscito (vedi meaning su ogni step)."
                ),
            }
        except Exception as exc:
            return {"ok": False, "mode": "move", "reason": repr(exc)}


def sport_simple_action(
    *,
    project_root: Path,
    domain: int,
    iface: str | None,
    action: str,
) -> dict[str, Any]:
    """Azioni Sport one-shot: stop, hello, stretch, sit, recovery, balance."""
    sc, err = _sport_client_ready(project_root, domain, iface)
    if sc is None:
        return err

    action = (action or "").strip().lower()
    handlers: dict[str, str] = {
        "stop": "stop",
        "stop_move": "stop",
        "hello": "hello",
        "stretch": "stretch",
        "sit": "sit",
        "recovery": "recovery",
        "balance": "balance",
    }
    if action not in handlers:
        return {"ok": False, "reason": f"unknown_action_{action!r}"}

    steps: dict[str, Any] = {}
    with _lock:
        try:
            if action in {"stop", "stop_move"}:
                code = sc.StopMove()
                steps["stop_move"] = {"code": code}
                code = sc.BalanceStand()
                steps["balance_stand"] = {"code": code}
            elif action == "hello":
                code, _ = sc.Hello()
                steps["hello"] = {"code": code}
            elif action == "stretch":
                code, _ = sc.Stretch()
                steps["stretch"] = {"code": code}
            elif action == "sit":
                code, _ = sc.Sit()
                steps["sit"] = {"code": code}
            elif action == "recovery":
                code, _ = sc.RecoveryStand()
                steps["recovery_stand"] = {"code": code}
            elif action == "balance":
                code, _ = sc.BalanceStand()
                steps["balance_stand"] = {"code": code}
            steps = _steps_with_meanings(steps)
            ok = _sport_steps_all_ok(steps)
            return {
                "ok": ok,
                "mode": action,
                "robot": "go2_quadrupede",
                "steps": steps,
                "hint": f"Sport {action} eseguito." if ok else f"Sport {action} fallito.",
            }
        except Exception as exc:
            return {"ok": False, "mode": action, "reason": repr(exc)}


if __name__ == "__main__":
    import argparse
    import json

    _cli_modes = (
        "crouch",
        "stand_up",
        "joystick",
        "damping",
        "balance_hold",
        "stop",
        "recovery_stand",
        "velocity",
    )
    ap = argparse.ArgumentParser(
        description="CLI Sport Go2: eseguire sulla macchina con Cyclone DDS verso il cane (es. Jetson sulla LAN Unitree)."
    )
    ap.add_argument(
        "mode",
        nargs="?",
        default="crouch",
        choices=_cli_modes,
        help="crouch = StopMove+StandDown; stand_up = StopMove+StandUp+BalanceStand (default: crouch)",
    )
    ap.add_argument("--vx", type=float, default=0.0, help="solo mode=velocity")
    ap.add_argument("--vy", type=float, default=0.0, help="solo mode=velocity")
    ap.add_argument("--vyaw", type=float, default=0.0, help="solo mode=velocity (yaw rate)")
    ap.add_argument(
        "--no-enable",
        action="store_true",
        help="Per joystick|damping|balance_hold: passa enable=false a sport_accompany.",
    )
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    domain = int(os.environ.get("GO2_DDS_DOMAIN", "0"))
    iface = (os.environ.get("GO2_DDS_INTERFACE") or "").strip() or None
    enable = not args.no_enable
    out = sport_accompany(
        project_root=root,
        domain=domain,
        iface=iface,
        enable=enable,
        mode=args.mode,
        vx=args.vx,
        vy=args.vy,
        vyaw=args.vyaw,
    )
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    raise SystemExit(0 if out.get("ok") else 1)

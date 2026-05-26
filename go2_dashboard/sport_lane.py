"""RPC Sport / stato ultimo sport — usato solo dalla dashboard operator (niente import ``diagnostics_dashboard``)."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT

_LOG = logging.getLogger(__name__)

GO2_DDS_DOMAIN = int(os.environ.get("GO2_DDS_DOMAIN", "0"))
GO2_DDS_INTERFACE = os.environ.get("GO2_DDS_INTERFACE", "").strip()

LAST_SPORT_RPC: dict[str, Any] = {
    "updated_at": None,
    "mode": None,
    "sync": None,
    "result": None,
    "error": None,
}
LAST_SPORT_RPC_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sport_record_last(*, mode: str, sync: bool, result: Any | None, error: str | None) -> None:
    with LAST_SPORT_RPC_LOCK:
        LAST_SPORT_RPC["updated_at"] = now_iso()
        LAST_SPORT_RPC["mode"] = mode
        LAST_SPORT_RPC["sync"] = sync
        LAST_SPORT_RPC["result"] = result
        LAST_SPORT_RPC["error"] = error


def base_motion_allowed() -> tuple[bool, str | None]:
    if os.environ.get("GO2_ENABLE_BASE_MOTION", "0").lower() not in {"1", "true", "yes"}:
        return (
            False,
            "GO2_ENABLE_BASE_MOTION is not enabled (refusing Sport RPC). Set GO2_ENABLE_BASE_MOTION=1 on the NX.",
        )
    if os.environ.get("GO2_LOCAL", "0").lower() not in {"1", "true", "yes"}:
        return False, "Dashboard must run on the robot with GO2_LOCAL=1 for Sport DDS."
    return True, None


def sport_stand_modes_use_subprocess() -> bool:
    return os.environ.get("GO2_SPORT_SUBPROCESS_STAND_MODES", "1").lower() in {"1", "true", "yes", "on"}


def sport_accompany_subprocess(
    *,
    mode: str,
    enable: bool,
    stand_up_first: bool,
    speed_level: int | None,
) -> dict[str, Any]:
    script = PROJECT_ROOT / "scripts" / "sport_accompany_once.py"
    if not script.is_file():
        return {"ok": False, "reason": "missing_scripts/sport_accompany_once.py", "mode": mode}
    cmd: list[str] = [
        sys.executable,
        str(script),
        "--mode",
        mode,
        "--enable",
        "1" if enable else "0",
        "--stand-up-first",
        "1" if stand_up_first else "0",
    ]
    if speed_level is not None:
        cmd.extend(["--speed-level", str(int(speed_level))])
    timeout_s = float(os.environ.get("GO2_SPORT_RPC_TIMEOUT_S", "55"))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "mode": mode,
            "reason": f"sport_subprocess_timeout_after_{timeout_s}s",
            "hint_it": "Il processo Sport non ha finito in tempo.",
        }
    stderr = (proc.stderr or "")[-4000:]
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        return {
            "ok": False,
            "mode": mode,
            "reason": f"sport_subprocess_exit_{proc.returncode}",
            "stderr_tail": stderr[-2500:],
        }
    try:
        out: dict[str, Any] = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {
            "ok": False,
            "mode": mode,
            "reason": "sport_subprocess_bad_json",
            "stdout": (proc.stdout or "")[:2000],
            "stderr": stderr[-1500:],
            "subprocess_returncode": proc.returncode,
        }
    if proc.returncode != 0 and not out.get("ok"):
        out["subprocess_returncode"] = proc.returncode
        if stderr.strip():
            out["stderr_tail"] = stderr[-2000:]
    return out


def accompany_execute_json(
    body: dict[str, Any],
    *,
    query_sync_flag: bool = False,
) -> tuple[dict[str, Any], int]:
    """Esegue Sport/DDS da un dizionario JSON (stesso contratto di POST ``/api/base/accompany_mode``)."""
    ok_gate, reason = base_motion_allowed()
    if not ok_gate:
        return {"ok": False, "reason": reason}, 403

    enable = bool(body.get("enable", True))
    stand_first = bool(body.get("stand_up_first", False))
    speed_raw = body.get("speed_level")
    speed_level = int(speed_raw) if speed_raw is not None else None

    iface = GO2_DDS_INTERFACE.strip() if GO2_DDS_INTERFACE else None
    mode = str(body.get("mode") or "joystick").strip().lower()
    dds_iface_report = iface if iface else None

    def _maybe_float(key: str) -> float | None:
        raw = body.get(key)
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    vx_f = _maybe_float("vx")
    vy_f = _maybe_float("vy")
    vyaw_f = _maybe_float("vyaw")
    pre_bal_raw = body.get("pre_balance")
    pre_balance = True if pre_bal_raw is None else bool(pre_bal_raw)

    def _sport_call() -> Any:
        if mode in {"crouch", "stand_up"} and sport_stand_modes_use_subprocess():
            return sport_accompany_subprocess(
                mode=mode,
                enable=enable,
                stand_up_first=stand_first,
                speed_level=speed_level,
            )
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from go2_accompany import sport_accompany

        return sport_accompany(
            project_root=PROJECT_ROOT,
            domain=GO2_DDS_DOMAIN,
            iface=iface,
            enable=enable,
            mode=mode,
            stand_up_first=stand_first,
            speed_level=speed_level,
            vx=vx_f,
            vy=vy_f,
            vyaw=vyaw_f,
            pre_balance=pre_balance,
        )

    sync = os.environ.get("GO2_SPORT_RPC_SYNC", "0").lower() in {"1", "true", "yes"}
    if query_sync_flag:
        sync = True
    if isinstance(body.get("sync"), bool) and body.get("sync"):
        sync = True
    async_stand = os.environ.get("GO2_SPORT_ASYNC_STAND_MODES", "0").lower() in {"1", "true", "yes"}
    if mode in {"crouch", "stand_up"} and not async_stand:
        sync = True

    if not sync:

        def _bg() -> None:
            try:
                result = _sport_call()
                sport_record_last(mode=mode, sync=False, result=result, error=None)
                _LOG.info("sport_accompany mode=%s ok=%s", mode, result.get("ok") if isinstance(result, dict) else result)
            except Exception as exc:
                sport_record_last(mode=mode, sync=False, result=None, error=repr(exc))
                _LOG.exception("sport_accompany mode=%s failed (background)", mode)

        threading.Thread(target=_bg, name=f"sport-{mode}", daemon=True).start()
        return (
            {
                "ok": True,
                "accepted": True,
                "async": True,
                "mode": mode,
                "dds_domain": GO2_DDS_DOMAIN,
                "dds_interface": dds_iface_report,
                "hint_it": "Sport RPC avviato in background sulla NX.",
            },
            202,
        )

    timeout_s = float(os.environ.get("GO2_SPORT_RPC_TIMEOUT_S", "45"))
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_sport_call)
            result = fut.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        sport_record_last(mode=mode, sync=True, result=None, error=f"sport_rpc_timeout_after_{timeout_s}s")
        return (
            {
                "ok": False,
                "reason": f"sport_rpc_timeout_after_{timeout_s}s",
                "hint": "DDS Sport ha tardato troppo — robot impegnato o DDS non raggiungibile.",
            },
            504,
        )
    except Exception as exc:
        sport_record_last(mode=mode, sync=True, result=None, error=repr(exc))
        return {"ok": False, "reason": repr(exc)}, 502

    sport_record_last(mode=mode, sync=True, result=result, error=None)
    status = 200 if result.get("ok") else 502
    return result, status


def accompany_mode_handle(request: Any) -> tuple[dict[str, Any], int]:
    """GET query o POST JSON verso ``accompany_execute_json``."""
    if request.method == "GET":
        if os.environ.get("GO2_ALLOW_GET_BASE_MOTION", "1").lower() not in {"1", "true", "yes", "on"}:
            return {"ok": False, "reason": "GET disabled (set GO2_ALLOW_GET_BASE_MOTION=1)"}, 405
        mq = (request.args.get("mode") or "").strip().lower()
        if not mq:
            return {"ok": False, "reason": "missing_query_parameter_mode"}, 400
        body: dict[str, Any] = {"mode": mq, "enable": True, "stand_up_first": False}
        if request.args.get("stand_up_first", "").lower() in {"1", "true", "yes"}:
            body["stand_up_first"] = True
        if request.args.get("enable", "").lower() in {"0", "false", "no"}:
            body["enable"] = False
        sl = request.args.get("speed_level")
        if sl is not None and str(sl).strip() != "":
            try:
                body["speed_level"] = int(sl)
            except ValueError:
                pass
        if request.args.get("sync", "").lower() in {"1", "true", "yes"}:
            body["sync"] = True
    else:
        body = request.get_json(silent=True) or {}

    qsync = request.args.get("sync", "").lower() in {"1", "true", "yes"}
    return accompany_execute_json(body, query_sync_flag=qsync)


def sport_last_payload() -> dict[str, Any]:
    with LAST_SPORT_RPC_LOCK:
        snap = {k: v for k, v in LAST_SPORT_RPC.items()}
    return {"ok": True, **snap}

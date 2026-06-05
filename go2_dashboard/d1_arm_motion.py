"""Movimento braccio D1 — delega a ``d1_jog.service`` (SDK) con fallback ``d1_arm_command``."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from typing import Any

from go2_dashboard.paths import (
    D1_ARM_COMMAND_BIN,
    D1_SDK_COMMAND_BIN,
    PROJECT_ROOT,
)
from go2_dashboard.sdk_backend import prefer_sdk_backend

_live_session_active = False


def is_live_session_active() -> bool:
    """True dopo ``begin_live_session`` (tab Giunti · Controllo live ON)."""
    return bool(_live_session_active)


def motion_backend_name() -> str:
    return "d1_sdk" if prefer_sdk_backend() else "d1_arm_command"


def command_binary_ready() -> bool:
    if prefer_sdk_backend():
        return D1_SDK_COMMAND_BIN.is_file() and os.access(D1_SDK_COMMAND_BIN, os.X_OK)
    return D1_ARM_COMMAND_BIN.is_file() and os.access(D1_ARM_COMMAND_BIN, os.X_OK)


def motion_gate_error(*, require_plan_execute: bool = False) -> dict[str, Any] | None:
    if os.environ.get("GO2_LOCAL", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "go2_local_off"}
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "GO2_ENABLE_REAL_ARM_off"}
    if require_plan_execute:
        allowed = False
        for key in (
            "GO2_ENABLE_ARM_PLAN_EXECUTE",
            "GO2_ENABLE_OPENVLA_ARM_EXECUTE",
            "GO2_ENABLE_GRASP_IK_EXECUTE",
        ):
            if os.environ.get(key, "0").lower() in {"1", "true", "yes", "on"}:
                allowed = True
                break
        if not allowed:
            return {
                "ok": False,
                "reason": "arm_plan_execute_disabled",
                "hint_it": "Serve uno tra GO2_ENABLE_ARM_PLAN_EXECUTE, GO2_ENABLE_OPENVLA_ARM_EXECUTE, GO2_ENABLE_GRASP_IK_EXECUTE.",
            }
    if not command_binary_ready():
        hint = "bash scripts/build_d1_sdk.sh" if prefer_sdk_backend() else "bash scripts/build_d1_arm_helpers.sh"
        return {
            "ok": False,
            "reason": "missing_arm_command_bin",
            "backend": motion_backend_name(),
            "hint_it": hint,
        }
    return None


def _auto_couple_enabled() -> bool:
    return os.environ.get("D1_AUTO_COUPLE_ON_MOVE", "1").lower() in {"1", "true", "yes", "on"}


def ensure_coupled_for_motion() -> dict[str, Any]:
    """Coppia motori (funcode 5) prima del primo movimento — come dashboard jog."""
    if not prefer_sdk_backend():
        return {"ok": True, "skipped": True, "reason": "legacy_backend"}
    from go2_dashboard.d1_jog import service as jog_svc

    if jog_svc._arm_coupled:  # noqa: SLF001 — stato condiviso col jog
        return {"ok": True, "skipped": True, "reason": "already_coupled"}
    if not _auto_couple_enabled():
        return {"ok": False, "reason": "not_coupled", "hint_it": "Abilita D1_AUTO_COUPLE_ON_MOVE=1 o usa Coppia ON (porta 5053)."}
    return jog_svc.arm_couple_once(force=False)


def ensure_grasp_motion_worker() -> dict[str, Any]:
    """Alias presa → stesso avvio del tab Giunti (``begin_operator_arm_session``)."""
    gate = motion_gate_error(require_plan_execute=True)
    if gate:
        return {**gate, "action": "ensure_grasp_motion_worker"}
    from go2_dashboard.operator_arm_motion import begin_operator_arm_session

    out = begin_operator_arm_session()
    out["action"] = "ensure_grasp_motion_worker"
    out["backend"] = motion_backend_name()
    return out


def begin_live_session(*, servo_deg: list[float] | None = None) -> dict[str, Any]:
    global _live_session_active
    if not prefer_sdk_backend():
        _live_session_active = True
        return {"ok": True, "backend": "legacy"}
    from go2_dashboard.d1_jog import service as jog_svc

    couple = ensure_coupled_for_motion()
    if not couple.get("ok") and not couple.get("skipped"):
        return couple
    out = jog_svc.joint_control_begin(servo_deg=servo_deg)
    if out.get("ok"):
        _live_session_active = True
    return out


def end_live_session() -> dict[str, Any]:
    global _live_session_active
    _live_session_active = False
    if not prefer_sdk_backend():
        return {"ok": True}
    from go2_dashboard.d1_jog import service as jog_svc

    return jog_svc.joint_control_end()


def kill_command_processes() -> dict[str, Any]:
    if os.name == "nt":
        return {"ok": True, "skipped": True, "reason": "pkill_unavailable_on_windows"}
    if prefer_sdk_backend():
        from go2_dashboard.d1_jog import service as jog_svc

        jog_svc.stop_command_daemon()
    patterns = ["d1_sdk_command", "d1_arm_command"]
    results: dict[str, Any] = {}
    for pat in patterns:
        try:
            r = subprocess.run(
                ["pkill", "-f", pat],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            results[pat] = {"ok": r.returncode in (0, 1), "returncode": r.returncode}
        except Exception as exc:
            results[pat] = {"ok": False, "detail": repr(exc)}
    return {"ok": True, "patterns": results}


def _legacy_cmd_run(stdin_payload: str, delay_ms: int, timeout_s: float) -> subprocess.CompletedProcess[str]:
    helper = D1_ARM_COMMAND_BIN
    env_sh = PROJECT_ROOT / "scripts" / "nx_dashboard_env.sh"
    domain = int(os.environ.get("GO2_DDS_DOMAIN", "0"))
    cwd = str(PROJECT_ROOT)
    if os.name != "nt" and env_sh.is_file():
        script = (
            f"cd {shlex.quote(cwd)} && . {shlex.quote(str(env_sh))} && "
            f"exec {shlex.quote(str(helper))} {domain} {int(delay_ms)}"
        )
        return subprocess.run(
            ["bash", "-c", script],
            cwd=cwd,
            input=stdin_payload,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    return subprocess.run(
        [str(helper), str(domain), str(int(delay_ms))],
        cwd=cwd,
        input=stdin_payload,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def publish_messages(messages: list[dict[str, Any]], *, delay_ms: int) -> dict[str, Any]:
    """Pubblica sequenza funcode (traiettoria / hold / goto)."""
    if not messages:
        return {"ok": True, "count": 0}
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": True, "skipped": True, "reason": "dry_run", "count": len(messages)}

    if prefer_sdk_backend():
        from go2_dashboard.d1_jog import service as jog_svc

        couple = ensure_coupled_for_motion()
        if not couple.get("ok") and not couple.get("skipped"):
            return couple
        # Stream sul daemon persistente (liveliness DDS mantenuta) — evita il cedimento
        # motori del processo one-shot tra le fasi della presa. Stesso meccanismo del jog.
        return jog_svc.publish_trajectory_stream(messages, delay_ms=delay_ms)

    if not D1_ARM_COMMAND_BIN.is_file():
        return {"ok": False, "reason": "missing_d1_arm_command"}
    stdin = "\n".join(json.dumps(m, separators=(",", ":")) for m in messages) + "\n"
    timeout_s = max(8.0, (delay_ms / 1000.0 + 0.25) * len(messages))
    proc = _legacy_cmd_run(stdin, delay_ms, timeout_s)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "backend": "d1_arm_command",
        "stdout_tail": (proc.stdout or "")[-1200:],
        "stderr_tail": (proc.stderr or "")[-800:],
    }


def trajectory_include_couple() -> bool:
    """Evita un secondo funcode 5 se il braccio è già accoppiato sul daemon jog."""
    if not prefer_sdk_backend():
        return True
    from go2_dashboard.d1_jog import service as jog_svc

    return not bool(jog_svc._arm_coupled)  # noqa: SLF001


def build_fc2_trajectory_messages(
    path: list[list[float]],
    *,
    fc2_mode: int,
    include_couple: bool | None = None,
) -> list[dict[str, Any]]:
    seq = int(time.time()) % 100000
    messages: list[dict[str, Any]] = []
    do_couple = trajectory_include_couple() if include_couple is None else bool(include_couple)
    if do_couple:
        messages.append({"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 1}})
    for point in path:
        data = {f"angle{i}": point[i] for i in range(7)}
        data["mode"] = fc2_mode
        messages.append({"seq": seq + len(messages), "address": 1, "funcode": 2, "data": data})
    return messages


def publish_live_servo_deg7(servo_deg: list[float]) -> dict[str, Any]:
    """Slider live: daemon DDS + funcode 2 (no nuovo processo per tick)."""
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": True, "skipped": True, "reason": "GO2_ENABLE_REAL_ARM off (dry)"}
    gate = motion_gate_error()
    if gate:
        return gate

    sd = [round(max(-135.0, min(135.0, float(servo_deg[i]))), 3) for i in range(min(7, len(servo_deg)))]
    while len(sd) < 7:
        sd.append(sd[-1])

    if prefer_sdk_backend():
        from go2_dashboard.d1_jog import service as jog_svc

        global _live_session_active
        if not _live_session_active:
            begin = begin_live_session(servo_deg=sd)
            if not begin.get("ok"):
                if begin.get("reason") == "not_coupled":
                    with_en = jog_svc.jog_with_enable(sd)
                    with_en["mode"] = "live_with_enable"
                    if with_en.get("ok") or with_en.get("skipped"):
                        _live_session_active = True
                    return with_en
                return begin
        out = jog_svc.jog_pose_deg(sd, keep_lock=True)
        out["mode"] = "live_sdk_stream"
        out["target_servo_deg"] = sd
        out["backend"] = "d1_sdk"
        return out

    # Legacy: burst funcode su processo one-shot
    fc2_mode = int(os.environ.get("D1_LIVE_ANGLE_MODE") or os.environ.get("D1_FC2_STREAM_MODE") or "1")
    if fc2_mode not in (0, 1):
        fc2_mode = 1
    repeats = max(1, int(os.environ.get("D1_LIVE_REPEAT", "10")))
    post_hold = max(0, min(56, int(os.environ.get("D1_LIVE_POSTHOLD_REPEATS", "20") or "20")))
    delay_ms = max(10, int(os.environ.get("D1_LIVE_DELAY_MS", "26")))
    path: list[list[float]] = [sd] * (repeats + post_hold)
    msgs = build_fc2_trajectory_messages(path, fc2_mode=fc2_mode, include_couple=True)
    out = publish_messages(msgs, delay_ms=delay_ms)
    out["mode"] = "live_legacy_burst"
    out["target_servo_deg"] = sd
    return out


def emergency_stop_hold(servo_deg: list[float], *, repeats: int, delay_ms: int, fc2_mode: int) -> dict[str, Any]:
    kill = kill_command_processes()
    time.sleep(0.12)
    cur = [round(float(servo_deg[i]), 3) for i in range(7)]
    seq = int(time.time()) % 100000
    messages: list[dict[str, Any]] = [{"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 1}}]
    for i in range(repeats):
        data = {f"angle{j}": cur[j] for j in range(7)}
        data["mode"] = fc2_mode
        messages.append({"seq": seq + 1 + i, "address": 1, "funcode": 2, "data": data})
    pub = publish_messages(messages, delay_ms=delay_ms)
    pub["kill_helper"] = kill
    pub["hold_servo_deg_7"] = cur
    pub["mode"] = "arm_emergency_stop_hold"
    return pub

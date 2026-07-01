"""Client DDS braccio D1 — protocollo ufficiale (funcode da d1_sdk / doc Unitree)."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from typing import Any

from go2_dashboard.d1_jog.motion_guard import (
    claim_plane as motion_claim_plane,
    force_idle as motion_force_idle,
    release as motion_release,
    release_plane as motion_release_plane,
    status as motion_guard_status,
    try_acquire as motion_try_acquire,
)
from go2_dashboard.paths import D1_ARM_SERVO_READ_PY, D1_SDK_COMMAND_BIN, D1_SDK_FEEDBACK_BIN, PROJECT_ROOT

JOINT_LIMITS: list[tuple[float, float]] = [
    (-135.0, 135.0),
    (-90.0, 90.0),
    (-90.0, 90.0),
    (-135.0, 135.0),
    (-90.0, 90.0),
    (-135.0, 135.0),
    (0.0, 90.0),
]


def _real_arm_enabled() -> bool:
    return os.environ.get("D1_JOG_ENABLE_REAL_ARM", os.environ.get("GO2_ENABLE_REAL_ARM", "1")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _dds_domain() -> int:
    return int(os.environ.get("D1_DDS_DOMAIN", os.environ.get("GO2_DDS_DOMAIN", "0")))


def _cyclonedds_uri_for_iface(iface: str) -> str:
    name = (iface or "eth0").strip() or "eth0"
    return (
        "<CycloneDDS><Domain><General><Interfaces>"
        f'<NetworkInterface name="{name}" multicast="default" priority="default"/>'
        "</Interfaces></General></Domain></CycloneDDS>"
    )


def _subprocess_env() -> dict[str, str]:
    """Env per binari C++ DDS — interfaccia L2 obbligatoria sulla Jetson (eth0)."""
    env = os.environ.copy()
    if not (env.get("CYCLONEDDS_URI") or "").strip():
        iface = (env.get("GO2_DDS_INTERFACE") or env.get("D1_DDS_INTERFACE") or "eth0").strip()
        env["CYCLONEDDS_URI"] = _cyclonedds_uri_for_iface(iface)
    env.setdefault("GO2_DDS_INTERFACE", "eth0")
    return env


def _env_shell_prefix(cwd: str) -> str | None:
    if os.name == "nt":
        return None
    env_candidates = [
        PROJECT_ROOT / "scripts" / "nx_d1_jog_env.sh",
        PROJECT_ROOT / "scripts" / "nx_dashboard_env.sh",
    ]
    for env_sh in env_candidates:
        if env_sh.is_file():
            return f"cd {shlex.quote(cwd)} && . {shlex.quote(str(env_sh))} && "
    return None


def _run_bin(
    binary: str,
    args: list[str],
    *,
    stdin: str | None = None,
    timeout_s: float = 12.0,
) -> subprocess.CompletedProcess[str]:
    cwd = str(PROJECT_ROOT)
    prefix = _env_shell_prefix(cwd)
    if prefix:
        script = prefix + "exec " + " ".join(shlex.quote(x) for x in [binary, *args])
        return subprocess.run(
            ["bash", "-c", script],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    return subprocess.run(
        [binary, *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=_subprocess_env(),
    )


def binaries_status() -> dict[str, Any]:
    cmd = D1_SDK_COMMAND_BIN
    fb = D1_SDK_FEEDBACK_BIN
    return {
        "command_bin": str(cmd),
        "command_ok": cmd.is_file() and os.access(cmd, os.X_OK),
        "feedback_bin": str(fb),
        "feedback_ok": fb.is_file() and os.access(fb, os.X_OK),
        "real_arm": _real_arm_enabled(),
        "dds_domain": _dds_domain(),
        "build_hint": "bash scripts/build_d1_sdk.sh",
    }


def clamp_servo_deg(servo_deg: list[float]) -> list[float]:
    out: list[float] = []
    for i, val in enumerate(servo_deg[:7]):
        lo, hi = JOINT_LIMITS[i]
        out.append(round(max(lo, min(hi, float(val))), 3))
    while len(out) < 7:
        out.append(out[-1] if out else 0.0)
    return out


def _parse_feedback_stdout(stdout: str) -> list[float] | None:
    latest: list[float] | None = None
    for line in (stdout or "").splitlines():
        if line.startswith("servo_angles "):
            parts = line.split()[1:]
            if len(parts) >= 7:
                try:
                    latest = [float(x) for x in parts[:7]]
                except ValueError:
                    pass
        m = re.search(
            r"servo0_data:([-\d.]+).*servo1_data:([-\d.]+).*servo2_data:([-\d.]+).*"
            r"servo3_data:([-\d.]+).*servo4_data:([-\d.]+).*servo5_data:([-\d.]+).*servo6_data:([-\d.]+)",
            line,
        )
        if m:
            try:
                latest = [float(m.group(i)) for i in range(1, 8)]
            except ValueError:
                pass
    return latest


def read_servo_deg(*, fast: bool = False) -> dict[str, Any]:
    fb = D1_SDK_FEEDBACK_BIN
    if not fb.is_file():
        return {"ok": False, "reason": "missing_bin", "hint": binaries_status()["build_hint"]}
    if fast:
        listen_s = max(1, int(os.environ.get("D1_CART_FEEDBACK_S", "1")))
        timeout_s = float(os.environ.get("D1_CART_FEEDBACK_TIMEOUT_S", "2.5"))
    else:
        listen_s = max(1, int(os.environ.get("D1_JOG_FEEDBACK_S", "3")))
        timeout_s = float(os.environ.get("D1_JOG_FEEDBACK_TIMEOUT_S", "12"))
    try:
        result = _run_bin(
            str(fb),
            [str(_dds_domain()), str(listen_s)],
            timeout_s=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "feedback_timeout"}
    angles = _parse_feedback_stdout(result.stdout or "")
    base: dict[str, Any] = {
        "ok": angles is not None,
        "servo_deg": angles,
        "returncode": result.returncode,
        "dds_counts": next(
            (ln.strip() for ln in (result.stdout or "").splitlines() if ln.startswith("servo_count=")),
            None,
        ),
    }
    if angles is None:
        py_fb = D1_ARM_SERVO_READ_PY
        if py_fb.is_file():
            try:
                py_res = _run_bin(
                    "python3",
                    [str(py_fb), str(_dds_domain()), str(listen_s)],
                    timeout_s=timeout_s,
                )
                py_angles = _parse_feedback_stdout(py_res.stdout or "")
                if py_angles is not None:
                    set_servo_cache(py_angles)
                    mark_coupled_from_feedback()
                    return {
                        "ok": True,
                        "servo_deg": py_angles,
                        "returncode": py_res.returncode,
                        "dds_counts": next(
                            (
                                ln.strip()
                                for ln in (py_res.stdout or "").splitlines()
                                if ln.startswith("servo_count=")
                            ),
                            None,
                        ),
                        "fallback": "python_feedback",
                    }
            except subprocess.TimeoutExpired:
                pass
        base["reason"] = "no_servo_feedback"
        base["stderr_tail"] = (result.stderr or "")[-500:]
    else:
        set_servo_cache(angles)
        mark_coupled_from_feedback()
    return base


_cmd_daemon_lock = threading.RLock()
_cmd_daemon_proc: subprocess.Popen[str] | None = None
_cmd_daemon_delay_ms: int | None = None


def _spawn_cmd_daemon(delay_ms: int) -> subprocess.Popen[str]:
    """Publisher persistente — come ``d1_drag_follow_experimental`` (Popen diretto)."""
    cmd_bin = str(D1_SDK_COMMAND_BIN)
    args = [cmd_bin, str(_dds_domain()), str(delay_ms)]
    cwd = str(PROJECT_ROOT)
    prefix = _env_shell_prefix(cwd)
    if prefix:
        script = prefix + "exec " + " ".join(shlex.quote(x) for x in args)
        return subprocess.Popen(
            ["bash", "-lc", script],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=_subprocess_env(),
        )
    return subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=_subprocess_env(),
    )


def ensure_command_daemon(delay_ms: int | None = None) -> bool:
    """Processo ``d1_sdk_command`` persistente (evita 150 ms di init per ogni tick)."""
    from go2_dashboard.d1_jog import motion_profile

    global _cmd_daemon_proc, _cmd_daemon_delay_ms
    if not _real_arm_enabled():
        return True
    # Ignora delay_ms per-call: un solo daemon evita cedimento motori tra jog giunti/TCP.
    dm = motion_profile.daemon_delay_ms()
    _ = delay_ms
    with _cmd_daemon_lock:
        if _cmd_daemon_proc is not None and _cmd_daemon_proc.poll() is None:
            return True
        stop_command_daemon()
        if not D1_SDK_COMMAND_BIN.is_file():
            return False
        try:
            _cmd_daemon_proc = _spawn_cmd_daemon(dm)
            _cmd_daemon_delay_ms = dm
        except OSError:
            return False
        time.sleep(0.05)
        return _cmd_daemon_proc.poll() is None


def stop_command_daemon() -> None:
    global _cmd_daemon_proc, _cmd_daemon_delay_ms
    with _cmd_daemon_lock:
        proc = _cmd_daemon_proc
        _cmd_daemon_proc = None
        _cmd_daemon_delay_ms = None
    if proc is None:
        return
    if proc.poll() is None:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()


def publish_messages_stream(
    messages: list[dict[str, Any]], *, delay_ms: int | None = None
) -> dict[str, Any]:
    """Invio rapido su publisher DDS già avviato (jog continuo)."""
    if not messages:
        return {"ok": True, "count": 0}
    if not _real_arm_enabled():
        return {"ok": True, "skipped": True, "reason": "dry_run", "count": len(messages)}
    if not ensure_command_daemon(delay_ms):
        return {"ok": False, "reason": "daemon_start_failed"}
    with _cmd_daemon_lock:
        proc = _cmd_daemon_proc
        if proc is None or proc.poll() is not None:
            return {"ok": False, "reason": "daemon_dead"}
        try:
            assert proc.stdin is not None
            for msg in messages:
                proc.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            stop_command_daemon()
            return {"ok": False, "reason": "daemon_broken_pipe"}
    return {"ok": True, "count": len(messages), "stream": True}


def wait_cartesian_idle(*, timeout_s: float = 4.0) -> bool:
    """Attende fine rampa jog cartesiano."""
    try:
        from go2_dashboard.d1_jog import jog_stream
    except Exception:
        return True
    t0 = time.time()
    while jog_stream.is_motion_active() and (time.time() - t0) < timeout_s:
        time.sleep(0.05)
    return not jog_stream.is_motion_active()


def _halt_cartesian_stream(*, wait_idle: bool = False) -> None:
    """Disarma jog cartesiano — il thread non invia più comandi DDS."""
    try:
        from go2_dashboard.d1_jog import jog_stream

        jog_stream.halt_completely()
        if wait_idle:
            wait_cartesian_idle()
    except Exception:
        pass


def motion_reset() -> dict[str, Any]:
    """Reset motion guard / jog stream — NON chiude il daemon DDS (evita cedimento motori)."""
    _halt_cartesian_stream()
    motion_force_idle()
    return {"ok": True, "action": "motion_reset", **motion_guard_status()}


def _hold_after_motion_enabled() -> bool:
    return os.environ.get("D1_JOG_HOLD_AFTER_MOTION", "0").lower() in {"1", "true", "yes", "on"}


def motion_status() -> dict[str, Any]:
    return motion_guard_status()


def hold_pose_stream(*, servo_deg: list[float] | None = None) -> dict[str, Any]:
    """Solo funcode 2 sulla posa — mai funcode 5 / release (coppia già attiva)."""
    sd = clamp_servo_deg(servo_deg) if servo_deg is not None else None
    if sd is None:
        cached = get_servo_cache()
        if cached is None:
            return {"ok": True, "skipped": True, "reason": "no_pose", "action": "hold_pose"}
        sd = cached
    if not _arm_coupled:
        return {"ok": True, "skipped": True, "reason": "not_coupled", "action": "hold_pose"}
    out = _stream_pose_hold(sd, repeats=1)
    out["action"] = "hold_pose"
    set_servo_cache(sd)
    return out


def maintain_coupling_stream(*, servo_deg: list[float] | None = None) -> dict[str, Any]:
    """Alias: solo hold funcode 2 — non rinnova funcode 5."""
    out = hold_pose_stream(servo_deg=servo_deg)
    out["action"] = "maintain_coupling"
    return out


def page_handoff(*, servo_deg: list[float] | None = None) -> dict[str, Any]:
    """Cambio pagina: ferma jog, hold posa — mai couple/release."""
    _halt_cartesian_stream()
    motion_force_idle()
    out = hold_pose_stream(servo_deg=servo_deg)
    out["action"] = "page_handoff"
    return out


def _publish_messages(messages: list[dict[str, Any]], *, delay_ms: int) -> dict[str, Any]:
    """Comandi one-shot (zero) — processo separato; non chiude il daemon persistente."""
    _halt_cartesian_stream(wait_idle=True)
    if not _real_arm_enabled():
        return {"ok": True, "skipped": True, "reason": "dry_run", "messages": messages}
    cmd_bin = D1_SDK_COMMAND_BIN
    if not cmd_bin.is_file():
        return {"ok": False, "reason": "missing_command_bin", "hint": binaries_status()["build_hint"]}
    stdin = "\n".join(json.dumps(m, separators=(",", ":")) for m in messages) + "\n"
    timeout_s = max(8.0, (delay_ms / 1000.0 + 0.25) * len(messages))
    try:
        result = _run_bin(
            str(cmd_bin),
            [str(_dds_domain()), str(delay_ms)],
            stdin=stdin,
            timeout_s=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "command_timeout"}
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout_tail": (result.stdout or "")[-800:],
        "stderr_tail": (result.stderr or "")[-400:],
    }


_couple_last_ts: float = 0.0
_servo_cache: list[float] | None = None
_arm_coupled: bool = False


def arm_coupled() -> bool:
    return bool(_arm_coupled)


def _infer_coupled_on_feedback_enabled() -> bool:
    return os.environ.get("D1_INFER_COUPLED_ON_FEEDBACK", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def mark_coupled_from_feedback() -> bool:
    """Feedback DDS valido ⇒ braccio raggiungibile; non invia funcode 5."""
    global _arm_coupled, _couple_last_ts
    if not _infer_coupled_on_feedback_enabled():
        return False
    if _arm_coupled:
        return True
    _arm_coupled = True
    _couple_last_ts = time.time()
    return True


def set_servo_cache(servo_deg: list[float] | None) -> None:
    global _servo_cache
    if servo_deg is None:
        _servo_cache = None
        return
    _servo_cache = clamp_servo_deg(servo_deg)


def get_servo_cache() -> list[float] | None:
    return list(_servo_cache) if _servo_cache is not None else None


def merge_single_joint_jog(servo_deg: list[float], joint_index: int) -> list[float]:
    """Un solo giunto muove — base dalla cache, mai feedback DDS nel hot path."""
    ji = int(joint_index)
    base = get_servo_cache()
    if base is None:
        return clamp_servo_deg(servo_deg)
    merged = base[:]
    if 0 <= ji < len(merged):
        merged[ji] = float(servo_deg[ji])
    return clamp_servo_deg(merged)


def arm_couple_once(*, force: bool = False) -> dict[str, Any]:
    """Una sola volta funcode 5 mode 1 sul daemon (modello drag_follow)."""
    global _arm_coupled, _couple_last_ts
    if _arm_coupled and not force:
        return {"ok": True, "skipped": True, "reason": "already_coupled", "action": "arm_couple_once"}
    from go2_dashboard.d1_jog import motion_profile

    if not _real_arm_enabled():
        _arm_coupled = True
        return {"ok": True, "skipped": True, "reason": "dry_run", "action": "arm_couple_once"}
    delay_ms = motion_profile.daemon_delay_ms()
    if not ensure_command_daemon(delay_ms):
        return {"ok": False, "reason": "daemon_start_failed", "action": "arm_couple_once"}
    out = publish_messages_stream([_couple_enable_message()], delay_ms=delay_ms)
    if out.get("ok") or out.get("skipped"):
        _arm_coupled = True
        _couple_last_ts = time.time()
    out["action"] = "arm_couple_once"
    return out


def _couple_messages(*, with_power: bool, seq: int) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    if with_power:
        msgs.append({"seq": seq, "address": 1, "funcode": 6, "data": {"power": 1}})
    msgs.append({"seq": seq + 1, "address": 1, "funcode": 5, "data": {"mode": 1}})
    return msgs


def ensure_coupled(*, with_power: bool = False, force: bool = False) -> dict[str, Any]:
    """Coppia ON esplicita — funcode 5 mode 1 solo se serve; mai release automatico."""
    global _arm_coupled, _couple_last_ts
    if _arm_coupled and not force:
        return {
            "ok": True,
            "skipped": True,
            "reason": "already_coupled",
            "action": "ensure_coupled",
            "arm_coupled": True,
        }
    ok, busy = motion_try_acquire("admin")
    if not ok:
        return {"ok": False, "reason": busy, "action": "ensure_coupled"}
    try:
        if with_power and not _arm_coupled:
            seq = int(time.time()) % 100000
            out = _publish_messages(_couple_messages(with_power=True, seq=seq), delay_ms=100)
            if out.get("ok") or out.get("skipped"):
                _arm_coupled = True
                _couple_last_ts = time.time()
        else:
            out = arm_couple_once(force=force)
        out["action"] = "ensure_coupled"
        out["arm_coupled"] = bool(_arm_coupled)
        return out
    finally:
        motion_release("admin")


def ensure_coupled_for_motion() -> dict[str, Any]:
    """
    Prima di movimenti programmati (scan, waypoint): non chiedere Coppia ON se il
    feedback giunti è già valido; non reinviare funcode 5 se già in coppia.
    """
    global _arm_coupled
    if _arm_coupled:
        return {
            "ok": True,
            "skipped": True,
            "reason": "already_coupled",
            "action": "ensure_coupled_for_motion",
        }
    fb = read_servo_deg(fast=True)
    if fb.get("ok") and fb.get("servo_deg"):
        mark_coupled_from_feedback()
        from go2_dashboard.d1_jog import motion_profile

        ensure_command_daemon(motion_profile.daemon_delay_ms())
        return {
            "ok": True,
            "skipped": True,
            "reason": "inferred_from_feedback",
            "action": "ensure_coupled_for_motion",
            "arm_coupled": True,
        }
    return ensure_coupled(with_power=False, force=False)


def arm_power_on() -> dict[str, Any]:
    """funcode 6 — alimentazione motori braccio (power=1)."""
    seq = int(time.time()) % 100000
    msg = {"seq": seq, "address": 1, "funcode": 6, "data": {"power": 1}}
    return _publish_messages([msg], delay_ms=100)


def enable_all(*, mode: int = 1, with_power: bool = False) -> dict[str, Any]:
    """funcode 5 — abilita coppia motori (mode 1). Opzionale funcode 6 prima."""
    seq = int(time.time()) % 100000
    msgs: list[dict[str, Any]] = []
    if with_power:
        msgs.append({"seq": seq, "address": 1, "funcode": 6, "data": {"power": 1}})
    msgs.append({"seq": seq + 1, "address": 1, "funcode": 5, "data": {"mode": int(mode)}})
    return _publish_messages(msgs, delay_ms=100)


def motor_release() -> dict[str, Any]:
    """funcode 5 mode 0 — SOLO su richiesta esplicita utente (mai automatico)."""
    ok, busy = motion_try_acquire("admin")
    if not ok:
        return {"ok": False, "reason": busy, "action": "motor_release"}
    try:
        _halt_cartesian_stream(wait_idle=True)
        stop_command_daemon()
        motion_force_idle()
        global _couple_last_ts, _arm_coupled
        _couple_last_ts = 0.0
        _arm_coupled = False
        seq = int(time.time()) % 100000
        msg = {"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 0}}
        out = _publish_messages([msg], delay_ms=80)
        out["action"] = "motor_release"
        out["stream_halted"] = True
        out["explicit_only"] = True
        return out
    finally:
        motion_release("admin")


def hold_current_pose(
    *,
    repeats: int | None = None,
    delay_ms: int | None = None,
    servo_deg: list[float] | None = None,
    acquire_lock: bool = True,
) -> dict[str, Any]:
    """Mantiene coppia sulla posa corrente: funcode 5 mode 1 + funcode 2 (mai mode 0 / release)."""
    if acquire_lock:
        ok, busy = motion_try_acquire("hold")
        if not ok:
            return {"ok": False, "reason": busy, "action": "hold_current_pose"}
    try:
        return _hold_current_pose_impl(
            repeats=repeats, delay_ms=delay_ms, servo_deg=servo_deg
        )
    finally:
        if acquire_lock:
            motion_release("hold")


def _stream_pose_hold(servo_deg: list[float], *, repeats: int = 1) -> dict[str, Any]:
    """Mantiene posa con soli funcode 2 sul daemon — niente burst funcode 5."""
    from go2_dashboard.d1_jog import motion_profile

    cur = clamp_servo_deg(servo_deg)
    m = motion_profile.smooth_mode()
    delay_ms = motion_profile.stream_delay_ms()
    if not ensure_command_daemon(delay_ms):
        return {"ok": False, "reason": "daemon_start_failed", "action": "stream_pose_hold"}
    sent = 0
    for i in range(max(1, repeats)):
        seq = int(time.time()) % 100000 + i
        data: dict[str, Any] = {"mode": m}
        for idx in range(7):
            data[f"angle{idx}"] = cur[idx]
        pub = publish_messages_stream(
            [{"seq": seq, "address": 1, "funcode": 2, "data": data}],
            delay_ms=delay_ms,
        )
        if not (pub.get("ok") or pub.get("skipped")):
            return {**pub, "action": "stream_pose_hold", "snapshot_deg": cur}
        sent += 1
    return {"ok": True, "sent": sent, "action": "stream_pose_hold", "snapshot_deg": cur}


def _hold_current_pose_impl(
    *,
    repeats: int | None = None,
    delay_ms: int | None = None,
    servo_deg: list[float] | None = None,
    heavy: bool = False,
) -> dict[str, Any]:
    if servo_deg is not None and not heavy and not _hold_after_motion_enabled():
        rpt = repeats if repeats is not None else 1
        return _stream_pose_hold(servo_deg, repeats=rpt)
    rpt = repeats if repeats is not None else max(3, int(os.environ.get("D1_ZERO_HOLD_REPEATS", "8")))
    dms = delay_ms if delay_ms is not None else max(35, int(os.environ.get("D1_ZERO_HOLD_DELAY_MS", "55")))
    if servo_deg is None:
        fb = read_servo_deg(fast=False)
        if not fb.get("ok") or not fb.get("servo_deg"):
            return {"ok": False, "reason": "no_feedback_for_hold", "action": "hold_current_pose"}
        cur = fb["servo_deg"]
    else:
        cur = clamp_servo_deg(servo_deg)
    seq = int(time.time()) % 100000
    m = int(os.environ.get("D1_JOG_MODE", "0"))
    msgs: list[dict[str, Any]] = [{"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 1}}]
    for i in range(rpt):
        angles: dict[str, Any] = {"mode": m}
        for idx in range(7):
            angles[f"angle{idx}"] = cur[idx]
        msgs.append({"seq": seq + 1 + i, "address": 1, "funcode": 2, "data": angles})
    out = _publish_messages(msgs, delay_ms=dms)
    out["action"] = "hold_current_pose"
    out["hold_repeats"] = rpt
    out["snapshot_deg"] = cur
    return out


def go_zero() -> dict[str, Any]:
    """Solo funcode 7, attesa, poi hold — nessun altro comando durante il movimento."""
    ok, busy = motion_try_acquire("zero")
    if not ok:
        return {"ok": False, "reason": busy, "action": "go_zero"}
    try:
        _halt_cartesian_stream(wait_idle=True)
        seq = int(time.time()) % 100000
        msg = {"seq": seq, "address": 1, "funcode": 7}
        zero_out = _publish_messages([msg], delay_ms=120)
        settle_s = max(1.0, float(os.environ.get("D1_ZERO_SETTLE_S", "4.0")))
        time.sleep(settle_s)
        hold_out = _hold_current_pose_impl(heavy=True)
        out: dict[str, Any] = {
            **zero_out,
            "funcode": 7,
            "action": "go_zero",
            "settle_s": settle_s,
            "hold_after_zero": hold_out,
            "coupling_maintained": bool(hold_out.get("ok") or hold_out.get("skipped")),
        }
        if not hold_out.get("ok") and not hold_out.get("skipped"):
            out["reason"] = hold_out.get("reason", "hold_after_zero_failed")
        return out
    finally:
        motion_release("zero")


def _pose_message(servo_deg: list[float], *, mode: int | None = None, seq: int | None = None) -> dict[str, Any]:
    from go2_dashboard.d1_jog import motion_profile

    sd = clamp_servo_deg(servo_deg)
    s = int(seq if seq is not None else time.time()) % 100000
    m = int(mode if mode is not None else motion_profile.smooth_mode())
    data: dict[str, Any] = {"mode": m}
    for i, ang in enumerate(sd):
        data[f"angle{i}"] = ang
    return {"seq": s, "address": 1, "funcode": 2, "data": data}


def _couple_enable_message(seq: int | None = None) -> dict[str, Any]:
    s = int(seq if seq is not None else time.time()) % 100000
    return {"seq": s, "address": 1, "funcode": 5, "data": {"mode": 1}}


def jog_pose_deg(
    servo_deg: list[float],
    *,
    mode: int | None = None,
    keep_lock: bool = False,
) -> dict[str, Any]:
    """Solo funcode 2 sul daemon DDS — niente nuovo processo per tick."""
    if not keep_lock:
        ok, busy = motion_try_acquire("joint")
        if not ok:
            return {"ok": False, "reason": busy, "action": "jog_pose"}
    try:
        st = motion_guard_status()
        if st.get("plane") != "joint":
            _halt_cartesian_stream()
        sd = clamp_servo_deg(servo_deg)
        from go2_dashboard.d1_jog import motion_profile

        if not _arm_coupled:
            return {"ok": False, "reason": "not_coupled", "hint": "Premi Coppia ON", "action": "jog_pose"}
        msgs = [_pose_message(sd)]
        delay_ms = motion_profile.joint_cmd_delay_ms()
        ensure_command_daemon(delay_ms)
        out = publish_messages_stream(msgs, delay_ms=delay_ms)
        set_servo_cache(sd)
        out["target_servo_deg"] = sd
        out["funcode"] = 2
        out["mode"] = msgs[-1]["data"]["mode"]
        out["action"] = "jog_pose"
        out["stream"] = True
        return out
    finally:
        if not keep_lock:
            motion_release("joint")


def joint_control_begin(*, servo_deg: list[float] | None = None) -> dict[str, Any]:
    _halt_cartesian_stream(wait_idle=True)
    ok, busy = motion_claim_plane("joint")
    if not ok:
        return {"ok": False, "reason": busy, "action": "joint_begin"}
    ok2, busy2 = motion_try_acquire("joint")
    if not ok2:
        motion_release_plane("joint")
        return {"ok": False, "reason": busy2, "action": "joint_begin"}
    if servo_deg is not None:
        set_servo_cache(servo_deg)
    from go2_dashboard.d1_jog import motion_profile

    ensure_command_daemon(motion_profile.daemon_delay_ms())
    couple = ensure_coupled_for_motion()
    if not couple.get("ok"):
        return {
            "ok": False,
            "reason": couple.get("reason", "not_coupled"),
            "hint": couple.get("hint") or "Leggi da robot o premi Coppia ON",
            "action": "joint_begin",
        }
    return {"ok": True, "plane": "joint", "action": "joint_begin", "coupling": couple}


def joint_control_end() -> dict[str, Any]:
    motion_release("joint")
    motion_release_plane("joint")
    sd = get_servo_cache()
    hold: dict[str, Any] = {"ok": True, "skipped": True}
    if sd is not None and _arm_coupled:
        hold = _stream_pose_hold(sd, repeats=1)
    return {"ok": True, "action": "joint_end", "hold": hold}


def cartesian_begin_jog(**kwargs: Any) -> dict[str, Any]:
    _halt_cartesian_stream()
    ok, busy = motion_claim_plane("cartesian")
    if not ok:
        return {"ok": False, "reason": busy, "action": "cartesian_jog_start"}
    ok2, busy2 = motion_try_acquire("cartesian")
    if not ok2:
        motion_release_plane("cartesian")
        return {"ok": False, "reason": busy2, "action": "cartesian_jog_start"}
    from go2_dashboard.d1_jog import jog_stream

    sd = kwargs.get("servo_deg")
    if sd is not None:
        sd = clamp_servo_deg(sd)
        set_servo_cache(sd)
    else:
        cached = get_servo_cache()
        if cached is not None:
            sd = cached
            kwargs["servo_deg"] = sd
        else:
            fb = read_servo_deg(fast=True)
            if not fb.get("ok") or not fb.get("servo_deg"):
                motion_release("cartesian")
                motion_release_plane("cartesian")
                return {"ok": False, "reason": fb.get("reason", "no_feedback"), "action": "cartesian_jog_start"}
            sd = fb["servo_deg"]
            set_servo_cache(sd)
            kwargs["servo_deg"] = sd
    if not _arm_coupled:
        motion_release("cartesian")
        motion_release_plane("cartesian")
        return {
            "ok": False,
            "reason": "not_coupled",
            "hint": "Premi Coppia ON",
            "action": "cartesian_jog_start",
        }
    out = jog_stream.jog_start(**kwargs)
    if not out.get("ok"):
        motion_release("cartesian")
        motion_release_plane("cartesian")
    out["action"] = "cartesian_jog_start"
    return out


def cartesian_move_tcp(
    *,
    axis: str,
    sign: float,
    delta_mm: float,
) -> dict[str, Any]:
    """Movimento lineare TCP locale sulla NX (es. +100 mm), senza tick HTTP."""
    ok, busy = motion_claim_plane("cartesian")
    if not ok:
        return {"ok": False, "reason": busy, "action": "move_tcp_local"}
    ok2, busy2 = motion_try_acquire("cartesian")
    if not ok2:
        motion_release_plane("cartesian")
        return {"ok": False, "reason": busy2, "action": "move_tcp_local"}
    try:
        from go2_dashboard.d1_jog import tcp_motion

        _halt_cartesian_stream(wait_idle=True)
        return tcp_motion.move_tcp_axis_local(axis=axis, sign=sign, delta_mm=delta_mm)
    finally:
        motion_release("cartesian")
        motion_release_plane("cartesian")


def cartesian_end_jog(*, hold_after: bool = False) -> dict[str, Any]:
    from go2_dashboard.d1_jog import jog_stream

    out: dict[str, Any] = dict(jog_stream.jog_stop())
    wait_cartesian_idle(timeout_s=2.0)
    jog_stream.halt_completely()
    st = jog_stream.jog_status()
    sd = st.get("servo_deg") or get_servo_cache()
    if sd is not None:
        out["post_hold"] = hold_pose_stream(servo_deg=sd)
    out["coupling_maintained"] = bool(_arm_coupled)
    motion_release("cartesian")
    motion_release_plane("cartesian")
    out["action"] = "cartesian_jog_stop"
    return out


def jog_with_enable(servo_deg: list[float]) -> dict[str, Any]:
    """Enable + posa (prima volta o dopo stop)."""
    seq = int(time.time()) % 100000
    msgs = [
        {"seq": seq, "address": 1, "funcode": 6, "data": {"power": 1}},
        {"seq": seq + 1, "address": 1, "funcode": 5, "data": {"mode": 1}},
    ]
    sd = clamp_servo_deg(servo_deg)
    data: dict[str, Any] = {"mode": int(os.environ.get("D1_JOG_MODE", "1"))}
    for i, ang in enumerate(sd):
        data[f"angle{i}"] = ang
    msgs.append({"seq": seq + 2, "address": 1, "funcode": 2, "data": data})
    delay_ms = max(8, int(os.environ.get("D1_JOG_CMD_DELAY_MS", "20")))
    out = _publish_messages(msgs, delay_ms=delay_ms)
    out["target_servo_deg"] = sd
    return out

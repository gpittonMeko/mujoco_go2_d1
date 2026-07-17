"""Client DDS braccio D1 — protocollo ufficiale (funcode da d1_sdk / doc Unitree)."""

from __future__ import annotations

from collections import deque
import json
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from go2_dashboard import d1_hold_client
from go2_dashboard.d1_jog.motion_guard import (
    begin_safety_preempt,
    claim_plane as motion_claim_plane,
    end_safety_preempt,
    force_idle as motion_force_idle,
    release as motion_release,
    release_plane as motion_release_plane,
    safety_preempt_active,
    status as motion_guard_status,
    try_acquire as motion_try_acquire,
)
from go2_dashboard.paths import D1_ARM_SERVO_READ_PY, D1_SDK_COMMAND_BIN, D1_SDK_FEEDBACK_BIN, PROJECT_ROOT

TRUE_ZERO_POSE_PATH = PROJECT_ROOT / "data" / "true_zero_pose.json"


JOINT_LIMITS: list[tuple[float, float]] = [
    (-135.0, 135.0),
    # Il riferimento servo reale include l'offset di calibrazione: sulla posa
    # ripiegata verificata live J1=-93.2 e J2=90.2. Un clamp a +/-90 altera
    # perfino l'hold della posa corrente. Margine limitato a 5 gradi.
    (-95.0, 95.0),
    (-95.0, 95.0),
    (-135.0, 135.0),
    (-95.0, 95.0),
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
        "command_daemon": command_daemon_status(),
        "funcode_runtime": dict(_last_publish),
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


def _parse_arm_feedback_stdout(stdout: str) -> list[Any]:
    out: list[Any] = []
    for line in (stdout or "").splitlines():
        if not line.startswith("arm_feedback "):
            continue
        payload = line[len("arm_feedback ") :].strip()
        if not payload:
            continue
        try:
            out.append(json.loads(payload))
        except (TypeError, ValueError):
            out.append(payload)
    return out[-20:]


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
    arm_feedback = _parse_arm_feedback_stdout(result.stdout or "")
    base: dict[str, Any] = {
        "ok": angles is not None,
        "servo_deg": angles,
        "returncode": result.returncode,
        "dds_counts": next(
            (ln.strip() for ln in (result.stdout or "").splitlines() if ln.startswith("servo_count=")),
            None,
        ),
        "arm_feedback": arm_feedback,
        "arm_feedback_count": len(arm_feedback),
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
                py_arm_feedback = _parse_arm_feedback_stdout(py_res.stdout or "")
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
                        "arm_feedback": py_arm_feedback,
                        "arm_feedback_count": len(py_arm_feedback),
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
_cmd_daemon_started_at: float | None = None
_cmd_daemon_log = PROJECT_ROOT / "logs" / "d1_command_daemon.log"
_last_publish: dict[str, Any] = {
    "ok": None,
    "reason": None,
    "at": None,
    "count": 0,
    "funcodes": [],
    "path": None,
}
_recent_publishes: deque[dict[str, Any]] = deque(maxlen=80)


def _spawn_cmd_daemon(delay_ms: int) -> subprocess.Popen[str]:
    """Publisher persistente — come ``d1_drag_follow_experimental`` (Popen diretto)."""
    cmd_bin = str(D1_SDK_COMMAND_BIN)
    args = [cmd_bin, str(_dds_domain()), str(delay_ms)]
    cwd = str(PROJECT_ROOT)
    # Mantieni il modello Luca: un unico processo C++ avviato direttamente.
    # Il passaggio aggiunto attraverso ``bash -lc`` risorgentava l'env e
    # nascondeva l'uscita del publisher dietro un falso avvio riuscito.
    _cmd_daemon_log.parent.mkdir(parents=True, exist_ok=True)
    log_fp = _cmd_daemon_log.open("a", encoding="utf-8")
    try:
        return subprocess.Popen(
            args,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=log_fp,
            text=True,
            bufsize=1,
            env=_subprocess_env(),
        )
    finally:
        log_fp.close()


def command_daemon_status() -> dict[str, Any]:
    """Stato reale del publisher, non la sola presenza del binario."""
    if d1_hold_client.external_hold_enabled():
        status = d1_hold_client.status()
        return {
            **status,
            "external": True,
            "alive": bool(status.get("publisher_alive")),
            "pid": status.get("publisher_pid"),
            "delay_ms": None,
            "started_at_monotonic": None,
            "log": None,
            "log_tail": status.get("detail", "") if not status.get("ok") else "",
        }
    with _cmd_daemon_lock:
        proc = _cmd_daemon_proc
        returncode = proc.poll() if proc is not None else None
        alive = bool(proc is not None and returncode is None)
        tail = ""
        if not alive and _cmd_daemon_log.is_file():
            try:
                tail = _cmd_daemon_log.read_text(encoding="utf-8", errors="replace")[-1200:]
            except OSError:
                pass
        return {
            "alive": alive,
            "pid": proc.pid if proc is not None else None,
            "returncode": returncode,
            "delay_ms": _cmd_daemon_delay_ms,
            "started_at_monotonic": _cmd_daemon_started_at,
            "log": str(_cmd_daemon_log),
            "log_tail": tail,
        }


def _record_publish(messages: list[dict[str, Any]], *, ok: bool, reason: str | None, path: str) -> None:
    global _last_publish
    funcodes = sorted(
        {
            int(m.get("funcode"))
            for m in messages
            if isinstance(m, dict) and m.get("funcode") is not None
        }
    )
    pose_targets: list[list[float]] = []
    for message in messages:
        if int(message.get("funcode", -1)) != 2:
            continue
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        try:
            pose_targets.append([round(float(data[f"angle{i}"]), 3) for i in range(7)])
        except (KeyError, TypeError, ValueError):
            pass
    _last_publish = {
        "ok": bool(ok),
        "reason": reason,
        "at": time.time(),
        "count": len(messages),
        "funcodes": funcodes,
        "path": path,
        "last_pose_target_servo_deg": pose_targets[-1] if pose_targets else None,
    }
    _recent_publishes.append(
        {
            **_last_publish,
            "pose_target_count": len(pose_targets),
        }
    )


def runtime_safety_status() -> dict[str, Any]:
    return {
        "command_daemon": command_daemon_status(),
        "arm_coupled": arm_coupled(),
        "motion": motion_guard_status(),
        "safety_preempt_active": bool(safety_preempt_active()),
        "last_publish": dict(_last_publish),
        "recent_publishes": list(_recent_publishes)[-24:],
    }


def ensure_command_daemon(delay_ms: int | None = None) -> bool:
    """Processo ``d1_sdk_command`` persistente (evita 150 ms di init per ogni tick)."""
    from go2_dashboard.d1_jog import motion_profile

    global _cmd_daemon_proc, _cmd_daemon_delay_ms, _cmd_daemon_started_at
    if not _real_arm_enabled():
        return True
    if d1_hold_client.external_hold_enabled():
        return bool(d1_hold_client.status().get("ok"))
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
            _cmd_daemon_started_at = time.monotonic()
        except OSError:
            return False
        # ChannelFactory + publisher impiegano almeno 150 ms prima del loop.
        # A 50 ms un processo destinato a morire sembrava ancora sano.
        time.sleep(0.30)
        return _cmd_daemon_proc.poll() is None


def stop_command_daemon() -> None:
    if d1_hold_client.external_hold_enabled():
        return
    global _cmd_daemon_proc, _cmd_daemon_delay_ms, _cmd_daemon_started_at
    with _cmd_daemon_lock:
        proc = _cmd_daemon_proc
        _cmd_daemon_proc = None
        _cmd_daemon_delay_ms = None
        _cmd_daemon_started_at = None
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
        _record_publish(messages, ok=True, reason="dry_run", path="daemon_stream")
        return {"ok": True, "skipped": True, "reason": "dry_run", "count": len(messages)}
    if d1_hold_client.external_hold_enabled():
        out = d1_hold_client.publish(messages, delay_ms=int(delay_ms or 0))
        out.setdefault("count", len(messages) if out.get("ok") else 0)
        out["external_hold_daemon"] = True
        _record_publish(
            messages,
            ok=bool(out.get("ok")),
            reason=None if out.get("ok") else str(out.get("reason") or "hold_daemon_publish_failed"),
            path="external_hold_daemon",
        )
        return out
    if not ensure_command_daemon(delay_ms):
        _record_publish(messages, ok=False, reason="daemon_start_failed", path="daemon_stream")
        return {"ok": False, "reason": "daemon_start_failed"}
    with _cmd_daemon_lock:
        proc = _cmd_daemon_proc
        if proc is None or proc.poll() is not None:
            _record_publish(messages, ok=False, reason="daemon_dead", path="daemon_stream")
            return {"ok": False, "reason": "daemon_dead"}
        try:
            assert proc.stdin is not None
            for msg in messages:
                proc.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            stop_command_daemon()
            _record_publish(messages, ok=False, reason="daemon_broken_pipe", path="daemon_stream")
            return {"ok": False, "reason": "daemon_broken_pipe"}
        # Il vecchio codice rispondeva OK prima che il C++ eseguisse Write().
        time.sleep(max(0.06, min(0.25, ((_cmd_daemon_delay_ms or 0) / 1000.0) + 0.06)))
        returncode = proc.poll()
        if returncode is not None:
            status = command_daemon_status()
            _record_publish(messages, ok=False, reason="daemon_died_after_publish", path="daemon_stream")
            return {
                "ok": False,
                "reason": "daemon_died_after_publish",
                "returncode": returncode,
                "daemon": status,
            }
    _record_publish(messages, ok=True, reason=None, path="daemon_stream")
    return {"ok": True, "count": len(messages), "stream": True, "daemon": command_daemon_status()}


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
        _record_publish(messages, ok=True, reason="dry_run", path="oneshot_bin")
        return {"ok": True, "skipped": True, "reason": "dry_run", "messages": messages}
    if d1_hold_client.external_hold_enabled():
        return publish_messages_stream(messages, delay_ms=delay_ms)
    cmd_bin = D1_SDK_COMMAND_BIN
    if not cmd_bin.is_file():
        _record_publish(messages, ok=False, reason="missing_command_bin", path="oneshot_bin")
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
        _record_publish(messages, ok=False, reason="command_timeout", path="oneshot_bin")
        return {"ok": False, "reason": "command_timeout"}
    out = {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout_tail": (result.stdout or "")[-800:],
        "stderr_tail": (result.stderr or "")[-400:],
    }
    _record_publish(messages, ok=bool(out["ok"]), reason=(None if out["ok"] else "oneshot_failed"), path="oneshot_bin")
    return out


_couple_last_ts: float = 0.0
_servo_cache: list[float] | None = None
_arm_coupled: bool = False


def arm_coupled() -> bool:
    global _arm_coupled
    if d1_hold_client.external_hold_enabled() and _real_arm_enabled():
        _arm_coupled = bool(d1_hold_client.status().get("hold_active"))
    return bool(_arm_coupled)


def _infer_coupled_on_feedback_enabled() -> bool:
    # Ricevere angoli prova la connettività DDS, non che la coppia sia inserita.
    return os.environ.get("D1_INFER_COUPLED_ON_FEEDBACK", "0").strip().lower() not in (
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


def _current_servo_deg_for_save(*, fast: bool = True) -> list[float] | None:
    fb = read_servo_deg(fast=fast)
    if fb.get("ok") and isinstance(fb.get("servo_deg"), list) and len(fb["servo_deg"]) >= 7:
        return clamp_servo_deg([float(x) for x in fb["servo_deg"][:7]])
    cached = get_servo_cache()
    if cached is not None:
        return cached
    return None


def safe_zero_pose_from_servo(servo_deg: list[float] | None) -> list[float] | None:
    """Posa di transito sicura: J0 preservato, braccio ripiegato in alto, gripper preservato."""
    if servo_deg is None:
        return None
    cur = clamp_servo_deg(servo_deg)
    transit = cur[:]
    # Posa rannicchiata verificata live: J1 negativo, J2 positivo.
    transit[1] = float(os.environ.get("D1_ZERO_TRANSIT_J1_DEG", "-90"))
    transit[2] = float(os.environ.get("D1_ZERO_TRANSIT_J2_DEG", "90"))
    transit[3] = float(os.environ.get("D1_ZERO_TRANSIT_J3_DEG", "0"))
    # Sul D1 reale J4 si assesta a circa 4.7 deg; zero causava un falso
    # position_timeout prima della rotazione laterale, pur a braccio ripiegato.
    transit[4] = float(os.environ.get("D1_ZERO_TRANSIT_J4_DEG", "5"))
    transit[5] = float(os.environ.get("D1_ZERO_TRANSIT_J5_DEG", "0"))
    transit[6] = float(os.environ.get("D1_ZERO_TRANSIT_J6_DEG", str(cur[6] if len(cur) > 6 else 5.0)))
    return clamp_servo_deg(transit)


def safe_zero_pose() -> list[float] | None:
    """Legge la posa corrente e la converte nel transito ZERO sicuro."""
    return safe_zero_pose_from_servo(_current_servo_deg_for_save())


def true_zero_pose_info() -> dict[str, Any]:
    exists = TRUE_ZERO_POSE_PATH.is_file()
    out: dict[str, Any] = {"ok": True, "exists": exists, "path": str(TRUE_ZERO_POSE_PATH)}
    if exists:
        try:
            raw = json.loads(TRUE_ZERO_POSE_PATH.read_text(encoding="utf-8"))
            out["pose"] = raw
            arm = raw.get("arm") if isinstance(raw, dict) else None
            out["safe_transit"] = bool(isinstance(arm, dict) and arm.get("safe_transit"))
        except Exception as exc:
            out["ok"] = False
            out["reason"] = repr(exc)
    return out


def load_true_zero_pose() -> list[float] | None:
    if not TRUE_ZERO_POSE_PATH.is_file():
        return None
    try:
        raw = json.loads(TRUE_ZERO_POSE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    arm = raw.get("arm") if isinstance(raw.get("arm"), dict) else raw
    if not isinstance(arm, dict):
        return None
    if not bool(arm.get("safe_transit")):
        return None
    pose = arm.get("servo_deg")
    if not isinstance(pose, list) or len(pose) < 6:
        return None
    try:
        return clamp_servo_deg([float(x) for x in pose[:7]])
    except (TypeError, ValueError):
        return None


def save_true_zero_pose(*, servo_deg: list[float] | None = None) -> dict[str, Any]:
    cur = clamp_servo_deg(servo_deg) if servo_deg is not None else _current_servo_deg_for_save()
    if cur is None:
        return {"ok": False, "reason": "no_servo_feedback_for_true_zero"}
    transit = safe_zero_pose_from_servo(cur) or cur
    payload = {
        "label": "true_zero",
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "note": "SAFE ZERO POSE: transito alto con J0 preservato, per scan/presa/programmi.",
        "arm": {
            "feedback_ok": True,
            "servo_deg": transit,
            "joints_rad": [round(float(x) * 0.017453292519943295, 6) for x in transit[:6]],
            "gripper_deg": transit[6] if len(transit) > 6 else None,
            "safe_transit": True,
            "source": "go2_dashboard.d1_jog.service.save_true_zero_pose",
        },
    }
    TRUE_ZERO_POSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRUE_ZERO_POSE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    set_servo_cache(transit)
    return {"ok": True, "saved_to": str(TRUE_ZERO_POSE_PATH), "pose": payload}


def goto_true_zero_pose() -> dict[str, Any]:
    z = load_true_zero_pose() or safe_zero_pose()
    if z is None:
        return {"ok": False, "reason": "no_servo_feedback_for_true_zero"}
    try:
        from go2_dashboard.d1_jog import program_runner

        return program_runner.move_to_servo_deg_smooth(z)
    except Exception as exc:
        return {"ok": False, "reason": repr(exc)}


def request_emergency_hold(*, reason: str = "ui") -> dict[str, Any]:
    """Stop immediato: ferma i flussi e mantiene la posa corrente."""
    begin_safety_preempt(f"hold:{reason}")
    try:
        from go2_dashboard.d1_jog import program_runner

        stop = program_runner.request_stop()
    except Exception as exc:
        stop = {"ok": False, "reason": repr(exc)}
    try:
        _halt_cartesian_stream(wait_idle=True)
        motion_force_idle()
        pose = _current_servo_deg_for_save(fast=True)
        if pose is None:
            hold = {"ok": False, "reason": "no_feedback_for_hold"}
        else:
            # Safety path: HOLD ORA must not trust the software-coupled flag.
            # Always refresh power+couple and pose in one batch.
            hold = couple_and_hold_pose(pose, with_power=True, force=True, acquire_lock=False)
            hold["forced_power_couple_before_hold"] = True
        return {
            "ok": bool(hold.get("ok") or hold.get("skipped")),
            "action": "hold_now",
            "reason": reason,
            "stop": stop,
            "feedback": {"ok": pose is not None, "servo_deg": pose},
            "hold": hold,
            "safety_preempt": True,
        }
    finally:
        end_safety_preempt(source=f"hold:{reason}")


def micro_jog_current_pose(*, joint_index: int, delta_deg: float) -> dict[str, Any]:
    """Tiny single-joint move with hard limits and atomic hold semantics."""
    ji = int(joint_index)
    if ji < 0 or ji > 6:
        return {"ok": False, "reason": "joint_index_out_of_range", "allowed": [0, 6]}
    max_delta = min(1.0, max(0.05, float(os.environ.get("D1_SAFE_MICRO_JOG_MAX_DEG", "1.0"))))
    delta = float(delta_deg)
    if abs(delta) < 1e-6 or abs(delta) > max_delta:
        return {
            "ok": False,
            "reason": "delta_out_of_range",
            "max_abs_delta_deg": max_delta,
            "requested_delta_deg": delta,
        }
    feedback = read_servo_deg(fast=True)
    pose = feedback.get("servo_deg") if feedback.get("ok") else None
    if not isinstance(pose, list) or len(pose) < 7:
        return {"ok": False, "reason": "feedback_unavailable", "feedback": feedback}
    current = clamp_servo_deg([float(x) for x in pose[:7]])
    pre_hold = couple_and_hold_pose(current, with_power=True, force=True, acquire_lock=False)
    if not pre_hold.get("ok"):
        return {"ok": False, "reason": "pre_hold_failed", "pre_hold": pre_hold, "feedback": feedback}
    target = current[:]
    target[ji] = current[ji] + delta
    target = clamp_servo_deg(target)
    effective_delta = round(target[ji] - current[ji], 3)
    if abs(effective_delta) < 1e-6:
        return {
            "ok": False,
            "reason": "clamped_no_motion",
            "joint_index": ji,
            "current_servo_deg": current,
            "target_servo_deg": target,
            "pre_hold": pre_hold,
        }
    move = couple_and_hold_pose(target, with_power=True, force=True, acquire_lock=False)
    ok = bool(move.get("ok") or move.get("skipped"))
    return {
        "ok": ok,
        "action": "micro_jog_current_pose",
        "joint_index": ji,
        "requested_delta_deg": delta,
        "effective_delta_deg": effective_delta,
        "current_servo_deg": current,
        "target_servo_deg": target,
        "pre_hold": pre_hold,
        "move_hold": move,
        "feedback": feedback,
    }


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


def couple_and_hold_pose(
    servo_deg: list[float], *, with_power: bool = False, force: bool = True, acquire_lock: bool = True
) -> dict[str, Any]:
    """Carica prima il target hold, poi abilita coppia, poi ribadisce il target.

    Se abilitiamo la coppia prima di aggiornare la posa, il daemon/SDK può
    inseguire per un istante il vecchio target di hold. Questo è esattamente
    lo scatto osservabile "prima di holdare".
    """
    global _arm_coupled, _couple_last_ts
    if _arm_coupled and not force:
        return {"ok": True, "skipped": True, "reason": "already_coupled"}
    if acquire_lock:
        ok, busy = motion_try_acquire("admin")
        if not ok:
            return {"ok": False, "reason": busy, "action": "couple_and_hold_pose"}
    try:
        from go2_dashboard.d1_jog import motion_profile

        sd = clamp_servo_deg(servo_deg)
        seq = int(time.time()) % 100000
        hold_m = motion_profile.hold_mode()
        messages: list[dict[str, Any]] = []
        if with_power:
            messages.append({"seq": seq, "address": 1, "funcode": 6, "data": {"power": 1}})
        # Pose hold in mode0: evita di “congelare” un target mode1 da traiettoria.
        messages.append(_pose_message(sd, mode=hold_m, seq=seq + len(messages)))
        messages.append({"seq": seq + len(messages), "address": 1, "funcode": 5, "data": {"mode": 1}})
        messages.append(_pose_message(sd, mode=hold_m, seq=seq + len(messages)))
        out = publish_messages_stream(messages)
        if out.get("ok") or out.get("skipped"):
            _arm_coupled = True
            _couple_last_ts = time.time()
            set_servo_cache(sd)
        out["action"] = "couple_and_hold_pose"
        out["target_servo_deg"] = sd
        out["atomic_batch"] = True
        out["hold_order"] = "power_pose_couple_pose" if with_power else "pose_couple_pose"
        return out
    finally:
        if acquire_lock:
            motion_release("admin")


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
        seq = int(time.time()) % 100000
        messages = _couple_messages(with_power=with_power, seq=seq)
        out = publish_messages_stream(messages)
        if out.get("ok") or out.get("skipped"):
            _arm_coupled = True
            _couple_last_ts = time.time()
        out["action"] = "ensure_coupled"
        out["arm_coupled"] = bool(_arm_coupled)
        return out
    finally:
        motion_release("admin")


def ensure_coupled_for_motion() -> dict[str, Any]:
    """
    Prima di movimenti programmati (scan, waypoint): non chiedere Coppia ON se il
    feedback giunti è già valido; non reinviare funcode 5 se già in coppia.

    Nota compatibilità test legacy: in passato si usava
    ensure_coupled(with_power=False, force=False), ora forziamo enable+power.
    """
    if arm_coupled():
        return {
            "ok": True,
            "skipped": True,
            "reason": "continuous_hold_active",
            "action": "ensure_coupled_for_motion",
            "hold_daemon": command_daemon_status(),
        }
    feedback = read_servo_deg(fast=True)
    pose = feedback.get("servo_deg") if feedback.get("ok") else None
    if not isinstance(pose, list) or len(pose) < 7:
        return {
            "ok": False,
            "reason": "no_fresh_pose_for_atomic_couple",
            "action": "ensure_coupled_for_motion",
            "feedback": feedback,
        }
    out = couple_and_hold_pose(pose, with_power=True, force=True)
    if out.get("ok"):
        out["reason"] = "atomic_power_couple_hold_for_motion"
    out["action"] = "ensure_coupled_for_motion"
    return out


def arm_power_on() -> dict[str, Any]:
    """funcode 6 — alimentazione motori braccio (power=1)."""
    seq = int(time.time()) % 100000
    msg = {"seq": seq, "address": 1, "funcode": 6, "data": {"power": 1}}
    return _publish_messages([msg], delay_ms=100)


def enable_all(*, mode: int = 1, with_power: bool = False) -> dict[str, Any]:
    """funcode 5 — abilita coppia motori (mode 1). Opzionale funcode 6 prima."""
    if int(mode) == 1:
        return ensure_coupled(with_power=with_power, force=True)
    if int(mode) == 0:
        return motor_release()
    seq = int(time.time()) % 100000
    msgs: list[dict[str, Any]] = []
    if with_power:
        msgs.append({"seq": seq, "address": 1, "funcode": 6, "data": {"power": 1}})
    msgs.append({"seq": seq + 1, "address": 1, "funcode": 5, "data": {"mode": int(mode)}})
    return _publish_messages(msgs, delay_ms=100)


def motor_release() -> dict[str, Any]:
    """funcode 5 mode 0 — SOLO su richiesta esplicita utente (mai automatico)."""
    begin_safety_preempt("release")
    try:
        try:
            from go2_dashboard.d1_jog import program_runner

            stop = program_runner.request_stop()
        except Exception as exc:
            stop = {"ok": False, "reason": repr(exc)}
        _halt_cartesian_stream(wait_idle=True)
        motion_force_idle()
        global _couple_last_ts, _arm_coupled
        _couple_last_ts = 0.0
        _arm_coupled = False
        seq = int(time.time()) % 100000
        msg = {"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 0}}
        out = _publish_messages([msg], delay_ms=80)
        stop_command_daemon()
        out["action"] = "motor_release"
        out["stream_halted"] = True
        out["explicit_only"] = True
        out["preempted"] = True
        out["stop"] = stop
        return out
    finally:
        end_safety_preempt(source="release")


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
    """Mantiene posa con soli funcode 2 mode0 sul daemon — niente burst funcode 5."""
    from go2_dashboard.d1_jog import motion_profile

    cur = clamp_servo_deg(servo_deg)
    # HOLD statico = mode 0 (ciclo 10Hz). mode 1 solo per traiettorie in moto.
    m = motion_profile.hold_mode()
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
    from go2_dashboard.d1_jog import motion_profile

    seq = int(time.time()) % 100000
    m = motion_profile.hold_mode()
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
    if safety_preempt_active():
        return {"ok": False, "reason": "motion_preempted:safety", "action": "jog_pose"}
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
        if not ensure_command_daemon(delay_ms):
            return {"ok": False, "reason": "daemon_start_failed", "action": "jog_pose"}
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

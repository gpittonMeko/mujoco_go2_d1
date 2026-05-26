"""Pubblicazione comandi D1 via ``bin/d1_arm_command`` dalla dashboard **lite**.

Richiede ``GO2_LOCAL=1``, ``GO2_ENABLE_REAL_ARM=1`` e uno tra:
``GO2_ENABLE_ARM_PLAN_EXECUTE`` / ``GO2_ENABLE_OPENVLA_ARM_EXECUTE`` / ``GO2_ENABLE_GRASP_IK_EXECUTE``.

Traiettorie: ``D1_LITE_TRAJ_STEP_SCALE`` (default 0.5) riduce il passo massimo tra waypoint DDS
(meno a scatti, movimento più lungo). Valori 0.35–0.7 tipici in laboratorio.

Hold coppia: ``D1_TRAJ_POSTHOLD_REPEATS`` / ``D1_LIVE_POSTHOLD_REPEATS`` ripetono l’ultima posa via
funcode 2 (stessa logica prehold goto) per ridurre cedimenti tra burst DDS.

**Multi-angolo (funcode 2), campo ``mode`` (SDK Unitree D1):**
``0`` = smoothing piccolo per stream tipo 10 Hz; ``1`` = smoothing grande per traiettorie.
La dashboard usa ``D1_LIVE_ANGLE_MODE`` (default ``1``) per slider/live e ``D1_TRAJ_ANGLE_MODE``
(default ``1``) per interpolazioni goto/traiettoria/hold/E-stop. Imposta ``D1_LIVE_ANGLE_MODE=0`` solo
se vuoi lo smoothing «10 Hz» del SDK Unitree per stream molto fitto.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
import shlex
import subprocess
import sys
import time
from typing import Any

from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag
from go2_dashboard.paths import PROJECT_ROOT

# Stesso mount usato in ``operator_scene`` / ``openvla_runtime`` (base_link = arm + mount).
_MOUNT_BASE_LINK_M = (0.15, 0.0, 0.06)

# Snapshot ZERO e START su disco (stessi path della dashboard diagnostica completa).
TRUE_ZERO_POSE_PATH = PROJECT_ROOT / "data" / "true_zero_pose.json"
ALIGNMENT_START_PATH = PROJECT_ROOT / "data" / "start_alignment.json"

_DEFAULT_ZERO_TRANSITION_STEPS = [3.4, 1.7, 1.5, 2.0, 3.4, 3.8, 6.5]
_DEFAULT_START_ALIGN_STEPS = [2.2, 1.0, 0.9, 1.2, 2.2, 2.4, 4.0]


def arm_plan_execute_allowed() -> bool:
    """Uno tra questi env abilita movimento da piano (giunti FK o IK verso punto 3D)."""
    for key in (
        "GO2_ENABLE_ARM_PLAN_EXECUTE",
        "GO2_ENABLE_OPENVLA_ARM_EXECUTE",
        "GO2_ENABLE_GRASP_IK_EXECUTE",
    ):
        if os.environ.get(key, "0").lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def _require_lite_arm_motion() -> dict[str, Any] | None:
    """Vincoli comuni movimento DDS dalla dashboard lite. ``None`` se OK."""
    if os.environ.get("GO2_LOCAL", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "go2_local_off"}
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "GO2_ENABLE_REAL_ARM_off"}
    if not arm_plan_execute_allowed():
        return {
            "ok": False,
            "reason": "arm_plan_execute_disabled",
            "hint_it": "Serve uno tra GO2_ENABLE_ARM_PLAN_EXECUTE, GO2_ENABLE_OPENVLA_ARM_EXECUTE, GO2_ENABLE_GRASP_IK_EXECUTE.",
        }
    helper = PROJECT_ROOT / "bin" / "d1_arm_command"
    if not helper.is_file() or not os.access(helper, os.X_OK):
        return {"ok": False, "reason": "missing_d1_arm_command"}
    return None


def _d1_fc2_multijoint_mode_stream() -> int:
    """Campo ``data.mode`` per funcode 2: stream/slider (~10 Hz piccole variazioni). Default 1 (come prima)."""
    raw = (
        os.environ.get("D1_LIVE_ANGLE_MODE") or os.environ.get("D1_FC2_STREAM_MODE") or "1"
    ).strip()
    try:
        v = int(raw)
    except ValueError:
        return 0
    return v if v in (0, 1) else 1


def _d1_fc2_multijoint_mode_trajectory() -> int:
    """Campo ``data.mode`` per funcode 2: waypoint/traiettoria. Default 1 (SDK)."""
    raw = (
        os.environ.get("D1_TRAJ_ANGLE_MODE") or os.environ.get("D1_FC2_TRAJ_MODE") or "1"
    ).strip()
    try:
        v = int(raw)
    except ValueError:
        return 1
    return v if v in (0, 1) else 1


def _parse_max_step_deg_7(env_key: str, default_csv: str) -> list[float]:
    raw = (os.environ.get(env_key) or "").strip() or default_csv
    try:
        parts = [float(x.strip()) for x in raw.split(",") if x.strip() != ""]
    except ValueError:
        parts = [float(x.strip()) for x in default_csv.split(",")]
    while len(parts) < 7:
        parts.append(parts[-1] if parts else 4.0)
    return parts[:7]


def _file_pose_delay_ms(delay_ms: int | None) -> int:
    if delay_ms is not None:
        return max(70, int(delay_ms))
    v = (os.environ.get("D1_LITE_FILE_POSE_DELAY_MS") or os.environ.get("D1_EDITOR_MOVE_DELAY_MS") or "420").strip()
    try:
        return max(70, int(v))
    except ValueError:
        return 420


def _servo_deg7_from_arm_blob(blob: dict[str, Any]) -> list[float] | None:
    """Estrae 7 angoli servo (gradi) da dict ``arm`` / ``arm_at_start`` nei JSON di calibrazione."""
    if not isinstance(blob, dict):
        return None
    sd = blob.get("servo_deg")
    if isinstance(sd, list) and len(sd) >= 6:
        try:
            out = [round(max(-135.0, min(135.0, float(sd[i]))), 3) for i in range(min(len(sd), 7))]
        except (TypeError, ValueError):
            return None
        while len(out) < 7:
            g = blob.get("gripper_deg")
            if g is not None:
                try:
                    out.append(round(max(-135.0, min(135.0, float(g))), 3))
                except (TypeError, ValueError):
                    out.append(out[-1])
            else:
                out.append(out[-1])
        return out[:7]
    jr = blob.get("joints_rad")
    if isinstance(jr, list) and len(jr) >= 6:
        try:
            out = [round(max(-135.0, min(135.0, math.degrees(float(jr[i])))), 3) for i in range(6)]
        except (TypeError, ValueError):
            return None
        g = blob.get("gripper_deg")
        if g is not None:
            try:
                out.append(round(max(-135.0, min(135.0, float(g))), 3))
            except (TypeError, ValueError):
                out.append(out[-1])
        else:
            out.append(out[-1])
        return out
    return None


def _interpolate_deg(start: list[float], target: list[float], max_step_deg: list[float]) -> list[list[float]]:
    deltas = [abs(t - s) / max(step, 1e-6) for s, t, step in zip(start, target, max_step_deg)]
    count = max(1, int(math.ceil(max(deltas, default=1.0))))
    return [
        [round(s + (t - s) * (idx / count), 3) for s, t in zip(start, target)]
        for idx in range(1, count + 1)
    ]


def _d1_cmd_run(stdin_payload: str, delay_ms: int, timeout_s: float) -> subprocess.CompletedProcess[str]:
    helper = PROJECT_ROOT / "bin" / "d1_arm_command"
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


def _pkill_d1_arm_command_processes() -> dict[str, Any]:
    """Termina processi ``d1_arm_command`` ancora in esecuzione (best-effort, solo Linux)."""
    if os.name == "nt":
        return {"ok": True, "skipped": True, "reason": "pkill_unavailable_on_windows"}
    try:
        result = subprocess.run(
            ["pkill", "-f", "d1_arm_command"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        return {
            "ok": result.returncode in (0, 1),
            "returncode": result.returncode,
            "stderr_tail": (result.stderr or "")[-400:],
        }
    except Exception as exc:
        return {"ok": False, "reason": repr(exc)}


def arm_emergency_stop_hold() -> dict[str, Any]:
    """E-stop software: ``pkill`` su ``d1_arm_command`` + ripetizione comandi alla posa servo letta ora.

    Non sostituisce arresto hardware, telecomando Unitree o liberare la zona. Richiede
    ``GO2_LOCAL=1``, ``GO2_ENABLE_REAL_ARM=1``. Disattivabile con ``GO2_ENABLE_ARM_ESTOP_HTTP=0``.
    """
    if os.environ.get("GO2_LOCAL", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "go2_local_off"}
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "GO2_ENABLE_REAL_ARM_off"}
    if os.environ.get("GO2_ENABLE_ARM_ESTOP_HTTP", "1").lower() in {"0", "false", "no", "off"}:
        return {"ok": False, "reason": "arm_estop_http_disabled", "hint_it": "GO2_ENABLE_ARM_ESTOP_HTTP=0 sulla NX."}
    helper = PROJECT_ROOT / "bin" / "d1_arm_command"
    if not helper.is_file() or not os.access(helper, os.X_OK):
        return {"ok": False, "reason": "missing_d1_arm_command"}

    kill = _pkill_d1_arm_command_processes()
    time.sleep(0.12)
    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) < 7:
        return {
            "ok": False,
            "reason": "no_servo_feedback",
            "kill_helper": kill,
            "diag": diag,
            "hint_it": "pkill eseguito ma feedback servo assente — usa arresto sul robot.",
        }

    cur = [round(float(angles_fb[i]), 3) for i in range(7)]
    try:
        rpt = max(4, int(os.environ.get("D1_ESTOP_HOLD_REPEATS", "18").strip() or "18"))
    except ValueError:
        rpt = 18
    try:
        dms = max(50, int(os.environ.get("D1_ESTOP_HOLD_DELAY_MS", "85").strip() or "85"))
    except ValueError:
        dms = 85

    seq = int(time.time()) % 100000
    fc2_mode = _d1_fc2_multijoint_mode_trajectory()
    messages: list[dict[str, Any]] = [{"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 1}}]
    for i in range(rpt):
        data = {f"angle{j}": cur[j] for j in range(7)}
        data["mode"] = fc2_mode
        messages.append({"seq": seq + 1 + i, "address": 1, "funcode": 2, "data": data})

    stdin = "\n".join(json.dumps(m, separators=(",", ":")) for m in messages) + "\n"
    timeout_s = max(20.0, (dms / 1000.0 + 0.45) * len(messages))
    proc = _d1_cmd_run(stdin, dms, timeout_s)
    return {
        "ok": proc.returncode == 0,
        "mode": "arm_emergency_stop_hold",
        "returncode": proc.returncode,
        "kill_helper": kill,
        "hold_repeats": rpt,
        "hold_delay_ms": dms,
        "hold_servo_deg_7": cur,
        "stdout_tail": (proc.stdout or "")[-900:],
        "stderr_tail": (proc.stderr or "")[-600:],
        "feedback_diag": diag,
        "warning_it": "E-stop software: verifica fisicamente braccio e zona; ripeti se necessario.",
    }


def _publish_servo_deg_trajectory(
    target7_deg: list[float],
    *,
    delay_ms: int | None,
    start7_deg: list[float] | None = None,
    feedback_diag: dict[str, Any] | None = None,
    max_step_deg: list[float] | None = None,
) -> dict[str, Any]:
    """Interpola da feedback (o ``start7_deg``) a ``target7_deg`` e pubblica su DDS."""
    if start7_deg is not None and len(start7_deg) >= 7:
        current = [round(float(start7_deg[i]), 3) for i in range(7)]
        diag = feedback_diag or {}
    else:
        angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
        if angles_fb is None or len(angles_fb) < 7:
            return {"ok": False, "reason": "no_servo_feedback", "diag": diag}
        current = [round(float(angles_fb[i]), 3) for i in range(7)]
    tgt = [round(float(target7_deg[i]), 3) for i in range(7)]

    if max_step_deg is not None and len(max_step_deg) >= 1:
        msd = list(max_step_deg[:7])
        while len(msd) < 7:
            msd.append(msd[-1])
        steps = msd
    else:
        max_step_raw = (os.environ.get("GO2_OPENVLA_ARM_MAX_STEP_DEG") or "10,10,10,10,10,10,12").strip()
        try:
            steps = [float(x) for x in max_step_raw.split(",")]
        except ValueError:
            steps = [10.0] * 7
        while len(steps) < 7:
            steps.append(steps[-1])

    try:
        step_scale = float(os.environ.get("D1_LITE_TRAJ_STEP_SCALE", "0.5") or "0.5")
    except ValueError:
        step_scale = 0.5
    step_scale = max(0.12, min(1.0, step_scale))
    steps = [max(0.12, float(s) * step_scale) for s in steps]

    path = _interpolate_deg(current, tgt, steps)
    try:
        post_hold_n = int(os.environ.get("D1_TRAJ_POSTHOLD_REPEATS", "14") or "14")
    except ValueError:
        post_hold_n = 14
    post_hold_n = max(0, min(56, post_hold_n))
    if path:
        hold_angles = [round(float(path[-1][i]), 3) for i in range(7)]
    else:
        hold_angles = [round(float(tgt[i]), 3) for i in range(7)]
    for _ in range(post_hold_n):
        path.append(list(hold_angles))

    fc2_mode = _d1_fc2_multijoint_mode_trajectory()
    seq = int(time.time()) % 100000
    messages: list[dict[str, Any]] = [{"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 1}}]
    for point in path:
        data = {f"angle{i}": point[i] for i in range(7)}
        data["mode"] = fc2_mode
        messages.append({"seq": seq + len(messages), "address": 1, "funcode": 2, "data": data})

    dms = max(120, int(delay_ms if delay_ms is not None else int(os.environ.get("GO2_OPENVLA_ARM_DELAY_MS", "500"))))
    stdin = "\n".join(json.dumps(m, separators=(",", ":")) for m in messages) + "\n"
    timeout_s = max(25.0, (dms / 1000.0 + 0.55) * len(messages))
    proc = _d1_cmd_run(stdin, dms, timeout_s)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "path_points": len(path),
        "target_servo_deg_7": tgt,
        "stdout_tail": (proc.stdout or "")[-1200:],
        "stderr_tail": (proc.stderr or "")[-800:],
        "feedback_diag": diag,
    }


def goto_joints_rad_clamped_six(joints_rad: list[float]) -> dict[str, Any]:
    """Interpolazione feedback→target (6 giunti in rad, modello ``arm_kinematics_d1_template``). Pinza = angolo attuale."""
    if os.environ.get("GO2_LOCAL", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "go2_local_off"}
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "GO2_ENABLE_REAL_ARM_off"}
    if not arm_plan_execute_allowed():
        return {
            "ok": False,
            "reason": "arm_plan_execute_disabled",
            "hint_it": "Imposta GO2_ENABLE_ARM_PLAN_EXECUTE=1 (o GO2_ENABLE_OPENVLA_ARM_EXECUTE / GO2_ENABLE_GRASP_IK_EXECUTE).",
        }
    helper = PROJECT_ROOT / "bin" / "d1_arm_command"
    if not helper.is_file() or not os.access(helper, os.X_OK):
        return {"ok": False, "reason": "missing_d1_arm_command"}

    s_scripts = str(PROJECT_ROOT / "scripts")
    if s_scripts not in sys.path:
        sys.path.insert(0, s_scripts)
    from arm_kinematics_d1_template import J_LIMITS, clamp

    if len(joints_rad) < 6:
        return {"ok": False, "reason": "need_6_joint_radians"}
    q_pol = [clamp(float(joints_rad[i]), *J_LIMITS[i]) for i in range(6)]
    target_deg = [round(max(-135.0, min(135.0, math.degrees(q_pol[i]))), 3) for i in range(6)]

    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) < 7:
        return {"ok": False, "reason": "no_servo_feedback", "diag": diag}
    target7 = target_deg + [round(float(angles_fb[6]), 3)]
    out = _publish_servo_deg_trajectory(
        target7_deg=target7,
        delay_ms=None,
        start7_deg=[round(float(angles_fb[i]), 3) for i in range(7)],
        feedback_diag=diag,
    )
    out["target_joints_rad_6"] = [round(float(x), 5) for x in q_pol]
    out["target_servo_deg_6"] = target_deg
    return out


def goto_home_servo_deg(*, delay_ms: int | None = None) -> dict[str, Any]:
    """Interpola feedback → posa **home** sui 7 servo (gradi).

    Default tutti 0°. Override lista separata da virgole::

        D1_HOME_SERVO_DEG_7=0,0,0,0,0,0,0

    Stessi gate di sicurezza degli altri movimenti DDS (``GO2_LOCAL``, ``GO2_ENABLE_REAL_ARM``,
    uno tra i flag ``GO2_ENABLE_ARM_PLAN_EXECUTE`` / ``..._OPENVLA...`` / ``..._GRASP_IK...``).

    **Nota:** la posa ZERO registrata è in ``data/true_zero_pose.json`` — usa ``goto_true_zero_from_json()`` / pulsante «ZERO salvato», non necessariamente tutti 0°.
    """
    bad = _require_lite_arm_motion()
    if bad:
        return bad

    raw = (os.environ.get("D1_HOME_SERVO_DEG_7") or "0,0,0,0,0,0,0").strip()
    try:
        parts = [float(x.strip()) for x in raw.split(",") if x.strip() != ""]
    except ValueError:
        return {"ok": False, "reason": "bad_D1_HOME_SERVO_DEG_7", "hint_it": "Formato: sette numeri in gradi, separati da virgola."}
    if len(parts) < 7:
        return {"ok": False, "reason": "need_7_servo_degrees_in_D1_HOME_SERVO_DEG_7"}
    target7 = [round(max(-135.0, min(135.0, float(parts[i]))), 3) for i in range(7)]

    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) < 7:
        return {"ok": False, "reason": "no_servo_feedback", "diag": diag}

    out = _publish_servo_deg_trajectory(
        target7_deg=target7,
        delay_ms=delay_ms,
        start7_deg=[round(float(angles_fb[i]), 3) for i in range(7)],
        feedback_diag=diag,
    )
    out["mode"] = "home_servo_deg"
    out["home_servo_deg_7_config"] = target7
    return out


def goto_true_zero_from_json(*, delay_ms: int | None = None) -> dict[str, Any]:
    """Vai alla posa **ZERO** da ``data/true_zero_pose.json``."""
    bad = _require_lite_arm_motion()
    if bad:
        return bad
    if not TRUE_ZERO_POSE_PATH.is_file():
        return {
            "ok": False,
            "reason": "no_true_zero_json",
            "path": str(TRUE_ZERO_POSE_PATH),
            "hint_it": "Genera o copia data/true_zero_pose.json sulla NX (salvataggio ZERO da UI diagnostica o manuale).",
        }
    try:
        data = json.loads(TRUE_ZERO_POSE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "reason": "true_zero_json_read_failed", "detail": repr(exc)}
    if not isinstance(data, dict):
        return {"ok": False, "reason": "invalid_true_zero_json"}
    arm = data.get("arm") or data.get("arm_at_start")
    if not isinstance(arm, dict):
        return {"ok": False, "reason": "true_zero_json_no_arm"}
    t7 = _servo_deg7_from_arm_blob(arm)
    if t7 is None:
        return {
            "ok": False,
            "reason": "true_zero_json_no_servo_deg",
            "hint_it": "Servono servo_deg o joints_rad nel blob arm.",
        }
    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) < 7:
        return {"ok": False, "reason": "no_servo_feedback", "diag": diag}
    dms = _file_pose_delay_ms(delay_ms)
    default_z = ",".join(str(x) for x in _DEFAULT_ZERO_TRANSITION_STEPS)
    steps = _parse_max_step_deg_7("D1_ZERO_TRANSITION_MAX_STEP_DEG", default_z)
    out = _publish_servo_deg_trajectory(
        target7_deg=t7,
        delay_ms=dms,
        start7_deg=[round(float(angles_fb[i]), 3) for i in range(7)],
        feedback_diag=diag,
        max_step_deg=steps,
    )
    out["mode"] = "goto_true_zero_file"
    out["true_zero_file"] = str(TRUE_ZERO_POSE_PATH)
    return out


def goto_saved_start_from_json(*, delay_ms: int | None = None) -> dict[str, Any]:
    """Vai alla posa **START** salvata con AprilTag (``data/start_alignment.json`` → ``arm_at_start``)."""
    bad = _require_lite_arm_motion()
    if bad:
        return bad
    if not ALIGNMENT_START_PATH.is_file():
        return {
            "ok": False,
            "reason": "no_start_alignment_json",
            "path": str(ALIGNMENT_START_PATH),
            "hint_it": "Genera o copia data/start_alignment.json sulla NX (contiene arm_at_start).",
        }
    try:
        data = json.loads(ALIGNMENT_START_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "reason": "start_alignment_read_failed", "detail": repr(exc)}
    arm = data.get("arm_at_start") or {}
    if not arm.get("feedback_ok"):
        return {
            "ok": False,
            "reason": "saved_start_no_arm_feedback",
            "hint_it": "START senza arm_at_start / feedback_ok — rigenera start_alignment.json.",
        }
    t7 = _servo_deg7_from_arm_blob(arm)
    if t7 is None:
        return {"ok": False, "reason": "saved_start_no_servo_angles"}
    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) < 7:
        return {"ok": False, "reason": "no_servo_feedback", "diag": diag}
    dms = _file_pose_delay_ms(delay_ms)
    default_s = ",".join(str(x) for x in _DEFAULT_START_ALIGN_STEPS)
    steps = _parse_max_step_deg_7("D1_START_ALIGN_MAX_STEP_DEG", default_s)
    out = _publish_servo_deg_trajectory(
        target7_deg=t7,
        delay_ms=dms,
        start7_deg=[round(float(angles_fb[i]), 3) for i in range(7)],
        feedback_diag=diag,
        max_step_deg=steps,
    )
    out["mode"] = "goto_saved_start_file"
    out["start_alignment_file"] = str(ALIGNMENT_START_PATH)
    if isinstance(data.get("saved_at"), str):
        out["start_saved_at"] = data["saved_at"]
    return out


def goto_true_zero_then_saved_start_from_json(*, delay_ms: int | None = None) -> dict[str, Any]:
    """Due traiettorie: ZERO file poi START file."""
    z = goto_true_zero_from_json(delay_ms=delay_ms)
    if not z.get("ok"):
        return {**z, "failed_segment": "true_zero"}
    s = goto_saved_start_from_json(delay_ms=delay_ms)
    out: dict[str, Any] = {
        "ok": bool(s.get("ok")),
        "mode": "goto_true_zero_then_start_file",
        "segment_true_zero": z,
        "segment_saved_start": s,
    }
    if not s.get("ok"):
        out["reason"] = "second_segment_failed"
        out["failed_segment"] = "saved_start"
    return out


def goto_tool_target_base_link_m(xyz_base_link: list[float], *, delay_ms: int | None = None) -> dict[str, Any]:
    """IK verso punta utensile in ``base_link`` (stesso frame di ``grasp_display_base_link_m`` / viewer)."""
    if os.environ.get("GO2_LOCAL", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "go2_local_off"}
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "GO2_ENABLE_REAL_ARM_off"}
    if not arm_plan_execute_allowed():
        return {
            "ok": False,
            "reason": "arm_plan_execute_disabled",
            "hint_it": "Imposta GO2_ENABLE_ARM_PLAN_EXECUTE=1 (o GO2_ENABLE_GRASP_IK_EXECUTE) sulla NX e riavvia la dashboard.",
        }
    helper = PROJECT_ROOT / "bin" / "d1_arm_command"
    if not helper.is_file() or not os.access(helper, os.X_OK):
        return {"ok": False, "reason": "missing_d1_arm_command"}

    if len(xyz_base_link) < 3:
        return {"ok": False, "reason": "bad_xyz"}
    try:
        bl = [float(xyz_base_link[i]) for i in range(3)]
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad_xyz"}

    dz = float((os.environ.get("GO2_GRASP_IK_OFFSET_Z_BASE_LINK_M") or "0").strip() or "0.0")
    bl[2] += dz

    tip_arm = [bl[i] - float(_MOUNT_BASE_LINK_M[i]) for i in range(3)]

    if os.environ.get("D1_GOTO_PREHOLD", "1").lower() in {"1", "true", "yes", "on"}:
        publish_d1_hold_current_lite(
            repeats=max(6, int(os.environ.get("D1_GOTO_PREHOLD_REPEATS", "14"))),
            delay_ms=max(35, int(os.environ.get("D1_GOTO_PREHOLD_DELAY_MS", "55"))),
        )

    s_scripts = str(PROJECT_ROOT / "scripts")
    if s_scripts not in sys.path:
        sys.path.insert(0, s_scripts)
    from arm_kinematics_d1_template import ik_reach

    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) < 7:
        return {"ok": False, "reason": "no_servo_feedback", "diag": diag}
    q_seed = [math.radians(float(angles_fb[i])) for i in range(6)]

    q_sol = ik_reach(tip_arm[0], tip_arm[1], tip_arm[2], primary_seed=q_seed)
    if q_sol is None:
        return {
            "ok": False,
            "reason": "ik_failed",
            "hint_it": "Target fuori workspace o IK non converge — prova offset Z (GO2_GRASP_IK_OFFSET_Z_BASE_LINK_M) o nuovo piano.",
            "tip_arm_m": [round(x, 5) for x in tip_arm],
            "base_link_target_m": [round(x, 5) for x in bl],
        }

    target_deg = [round(max(-135.0, min(135.0, math.degrees(float(q_sol[i])))), 3) for i in range(6)]
    grip = angles_fb[6]
    raw_g = (os.environ.get("GO2_GRASP_IK_CLOSE_GRIPPER_DEG") or "").strip()
    if raw_g:
        try:
            grip = max(-135.0, min(135.0, float(raw_g)))
        except ValueError:
            pass
    target7 = target_deg + [round(float(grip), 3)]

    out = _publish_servo_deg_trajectory(
        target7_deg=target7,
        delay_ms=delay_ms,
        start7_deg=[round(float(angles_fb[i]), 3) for i in range(7)],
        feedback_diag=diag,
    )
    out["mode"] = "ik_base_link"
    out["ik_target_base_link_m"] = [round(x, 5) for x in bl]
    out["ik_tip_arm_m"] = [round(x, 5) for x in tip_arm]
    out["ik_solution_rad_6"] = [round(float(x), 5) for x in q_sol]
    return out


def pick_tool_target_base_link_m_from_plan(plan: dict[str, Any]) -> list[float] | None:
    """Sceglie un punto 3D ``base_link`` dal JSON dell'ultimo piano (priorità FK VLA poi marker)."""
    if not isinstance(plan, dict):
        return None
    for key in (
        "openvla_fk_tool_tip_base_link_m",
        "grasp_display_base_link_m",
        "grasp_center_base_link_m",
        "approach_point_base_link_m",
        "target_base_link_m",
    ):
        v = plan.get(key)
        if isinstance(v, (list, tuple)) and len(v) >= 3:
            try:
                return [float(v[0]), float(v[1]), float(v[2])]
            except (TypeError, ValueError):
                continue
    data = plan.get("data")
    if isinstance(data, dict):
        for key in ("grasp_display_base_link_m", "grasp_center_base_link_m"):
            v = data.get(key)
            if isinstance(v, (list, tuple)) and len(v) >= 3:
                try:
                    return [float(v[0]), float(v[1]), float(v[2])]
                except (TypeError, ValueError):
                    continue
    pts = plan.get("operators_grasp_points_base_link_m")
    if isinstance(pts, list) and pts:
        p0 = pts[0]
        if isinstance(p0, (list, tuple)) and len(p0) >= 3:
            try:
                return [float(p0[0]), float(p0[1]), float(p0[2])]
            except (TypeError, ValueError):
                return None
    return None


def current_tool_tip_base_link_m() -> tuple[list[float] | None, dict[str, Any]]:
    """FK posizione punta utensile in ``base_link`` (stesso frame di ``goto_tool_target_base_link_m``)."""
    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) < 7:
        return None, diag if isinstance(diag, dict) else {}
    s_scripts = str(PROJECT_ROOT / "scripts")
    if s_scripts not in sys.path:
        sys.path.insert(0, s_scripts)
    from arm_kinematics_d1_template import fk_tool_tip

    q_seed = [math.radians(float(angles_fb[i])) for i in range(6)]
    tip_arm = fk_tool_tip(q_seed)
    bl = [float(tip_arm[i]) + float(_MOUNT_BASE_LINK_M[i]) for i in range(3)]
    return [round(x, 5) for x in bl], diag


def goto_tool_target_base_link_m_partial(
    xyz_base_link: list[float],
    *,
    approach_blend: float,
    delay_ms: int | None = None,
) -> dict[str, Any]:
    """IK verso un **waypoint intermedio**: interpolazione lineare in ``base_link`` tra FK corrente e target."""
    cur, diag = current_tool_tip_base_link_m()
    if cur is None:
        return {"ok": False, "reason": "no_current_tip_fk", "diag": diag}
    if len(xyz_base_link) < 3:
        return {"ok": False, "reason": "bad_xyz"}
    try:
        tgt = [float(xyz_base_link[i]) for i in range(3)]
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad_xyz"}
    try:
        ab = float(approach_blend)
    except (TypeError, ValueError):
        ab = 0.22
    try:
        mx = float(os.environ.get("GO2_GRASP_COACH_MAX_APPROACH_BLEND", "0.28") or "0.28")
    except ValueError:
        mx = 0.28
    mx = max(0.06, min(mx, 0.45))
    ab = max(0.04, min(mx, ab))
    partial = [cur[i] + ab * (tgt[i] - cur[i]) for i in range(3)]
    inner = dict(goto_tool_target_base_link_m(partial, delay_ms=delay_ms))
    inner["mode"] = "ik_base_link_partial"
    inner["approach_blend_applied"] = ab
    inner["target_full_base_link_m"] = [round(tgt[i], 5) for i in range(3)]
    inner["waypoint_base_link_m"] = [round(partial[i], 5) for i in range(3)]
    inner["current_tip_base_link_m"] = cur
    return inner


# --- Movimenti manuali UI (slider / editor): stessi gate DDS della monolite per comandi diretti,
# senza richiedere GO2_ENABLE_ARM_PLAN_EXECUTE (solo GO2_LOCAL + GO2_ENABLE_REAL_ARM + helper).

_DEFAULT_EDITOR_STEPS = [1.6, 0.8, 0.7, 1.0, 1.6, 1.8, 4.0]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_step_deg_csv(raw: str | None, fallback: list[float]) -> list[float]:
    if not (raw or "").strip():
        return list(fallback)
    try:
        parts = [float(x.strip()) for x in str(raw).split(",") if x.strip() != ""]
    except ValueError:
        return list(fallback)
    if not parts:
        return list(fallback)
    while len(parts) < 7:
        parts.append(parts[-1])
    return parts[:7]


def editor_max_step_deg_7() -> list[float]:
    return parse_step_deg_csv(os.environ.get("D1_EDITOR_MAX_STEP_DEG"), _DEFAULT_EDITOR_STEPS)


def _manual_dds_gate() -> dict[str, Any] | None:
    if os.environ.get("GO2_LOCAL", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "skipped": False, "reason": "go2_local_off"}
    helper = PROJECT_ROOT / "bin" / "d1_arm_command"
    if not helper.is_file() or not os.access(helper, os.X_OK):
        return {"ok": False, "skipped": False, "reason": "missing_d1_arm_command"}
    return None


def publish_d1_hold_current_lite(*, repeats: int | None = None, delay_ms: int | None = None) -> dict[str, Any]:
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": True, "skipped": True, "reason": "GO2_ENABLE_REAL_ARM off (dry)"}
    bad = _manual_dds_gate()
    if bad:
        return bad
    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) < 7:
        return {"ok": False, "reason": "no_servo_feedback", "diag": diag}
    rpt = repeats if repeats is not None else max(6, int(os.environ.get("D1_GOTO_PREHOLD_REPEATS", "14")))
    dms = delay_ms if delay_ms is not None else max(35, int(os.environ.get("D1_GOTO_PREHOLD_DELAY_MS", "55")))
    cur = [round(float(angles_fb[i]), 3) for i in range(7)]
    seq = int(time.time()) % 100000
    messages: list[dict[str, Any]] = [{"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 1}}]
    fc2_mode = _d1_fc2_multijoint_mode_trajectory()
    for i in range(max(1, int(rpt))):
        data = {f"angle{j}": cur[j] for j in range(7)}
        data["mode"] = fc2_mode
        messages.append({"seq": seq + 1 + i, "address": 1, "funcode": 2, "data": data})
    stdin = "\n".join(json.dumps(m, separators=(",", ":")) for m in messages) + "\n"
    timeout_s = max(20.0, (dms / 1000.0 + 0.45) * len(messages))
    proc = _d1_cmd_run(stdin, dms, timeout_s)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "mode": "hold_current",
        "stdout_tail": (proc.stdout or "")[-600:],
        "stderr_tail": (proc.stderr or "")[-400:],
    }


def publish_live_pose_deg7(servo_deg: list[float]) -> dict[str, Any]:
    """Raffica funcode 5 + 2 come slider real-time della dashboard monolite.

    Tra una richiesta e la successiva il processo DDS termina: se il firmware non mantiene da solo
    la coppia senza messaggi su ``rt/arm_Command``, lunghi vuoti possono far **collassare** il braccio.
    ``D1_LIVE_PREHOLD`` riduce il tempo «aperto» dopo lettura feedback; vedi anche fix lettura servo
    (uscita rapida da ``d1_arm_feedback_helper``).
    """
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": True, "skipped": True, "reason": "GO2_ENABLE_REAL_ARM off (dry)"}
    bad = _manual_dds_gate()
    if bad:
        return bad
    sd_in = [float(servo_deg[i]) for i in range(min(7, len(servo_deg)))]
    while len(sd_in) < 7:
        sd_in.append(sd_in[-1])
    sd = [round(max(-135.0, min(135.0, sd_in[i])), 3) for i in range(7)]

    pre_meta: dict[str, Any] = {}
    if os.environ.get("D1_LIVE_PREHOLD", "1").lower() in {"1", "true", "yes", "on"}:
        try:
            prpt = int(os.environ.get("D1_LIVE_PREHOLD_REPEATS", "10") or "10")
        except ValueError:
            prpt = 10
        try:
            pdms = int(os.environ.get("D1_LIVE_PREHOLD_DELAY_MS", "48") or "48")
        except ValueError:
            pdms = 52
        prpt = max(4, min(40, prpt))
        pdms = max(28, min(130, pdms))
        pre_meta = dict(publish_d1_hold_current_lite(repeats=prpt, delay_ms=pdms))
        pre_meta["live_prehold_repeats"] = prpt
        pre_meta["live_prehold_delay_ms"] = pdms

    repeats = max(1, int(os.environ.get("D1_LIVE_REPEAT", "10")))
    delay_ms = max(10, int(os.environ.get("D1_LIVE_DELAY_MS", "26")))
    try:
        post_hold = max(0, int(os.environ.get("D1_LIVE_POSTHOLD_REPEATS", "20") or "20"))
    except ValueError:
        post_hold = 20
    try:
        post_cap = int(os.environ.get("D1_LIVE_POSTHOLD_CAP", "56") or "56")
    except ValueError:
        post_cap = 56
    post_cap = max(12, min(96, post_cap))
    post_hold = min(post_hold, post_cap)
    fc2_live = _d1_fc2_multijoint_mode_stream()
    seq = int(time.time()) % 100000
    messages: list[dict[str, Any]] = [{"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 1}}]
    angles = {f"angle{idx}": sd[idx] for idx in range(7)}
    angles["mode"] = fc2_live
    for r in range(repeats):
        messages.append({"seq": seq + 1 + r, "address": 1, "funcode": 2, "data": dict(angles)})
    for r in range(post_hold):
        messages.append(
            {"seq": seq + 1 + repeats + r, "address": 1, "funcode": 2, "data": dict(angles)}
        )
    stdin = "\n".join(json.dumps(m, separators=(",", ":")) for m in messages) + "\n"
    timeout_s = max(25.0, (delay_ms / 1000.0 + 0.4) * len(messages))
    proc = _d1_cmd_run(stdin, delay_ms, timeout_s)
    ok = proc.returncode == 0
    out: dict[str, Any] = {
        "ok": ok,
        "skipped": False,
        "mode": "live_direct",
        "target_servo_deg": sd,
        "live_burst_repeats": repeats,
        "live_fc2_angle_mode": fc2_live,
        "live_posthold_repeats": post_hold,
        "returncode": proc.returncode,
        "stdout_tail": (proc.stdout or "")[-900:],
        "stderr_tail": (proc.stderr or "")[-600:],
    }
    if pre_meta:
        out["live_prehold"] = pre_meta
    return out


def publish_goto_servo_deg7(
    servo_deg: list[float],
    *,
    max_step_deg: list[float] | None = None,
    delay_ms: int | None = None,
    skip_prehold: bool = False,
) -> dict[str, Any]:
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": True, "skipped": True, "reason": "GO2_ENABLE_REAL_ARM off (dry)"}
    bad = _manual_dds_gate()
    if bad:
        return bad
    sd = [float(servo_deg[i]) for i in range(min(7, len(servo_deg)))]
    while len(sd) < 7:
        sd.append(sd[-1])
    sd = [round(max(-135.0, min(135.0, sd[i])), 3) for i in range(7)]

    if (
        not skip_prehold
        and os.environ.get("D1_GOTO_PREHOLD", "1").lower() in {"1", "true", "yes"}
    ):
        publish_d1_hold_current_lite(
            repeats=max(6, int(os.environ.get("D1_GOTO_PREHOLD_REPEATS", "14"))),
            delay_ms=max(35, int(os.environ.get("D1_GOTO_PREHOLD_DELAY_MS", "55"))),
        )

    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) < 7:
        return {"ok": False, "skipped": False, "reason": "no_servo_feedback", "diag": diag}

    steps_final = max_step_deg if max_step_deg is not None else editor_max_step_deg_7()
    try:
        dms = int(delay_ms if delay_ms is not None else os.environ.get("D1_EDITOR_MOVE_DELAY_MS", "420"))
    except ValueError:
        dms = 420
    dms = max(70, dms)

    out = _publish_servo_deg_trajectory(
        target7_deg=sd,
        delay_ms=dms,
        start7_deg=[round(float(angles_fb[i]), 3) for i in range(7)],
        feedback_diag=diag,
        max_step_deg=steps_final,
    )
    out["skipped"] = False
    out["target_servo_deg"] = sd
    out["stage_name"] = "editor_goto"
    return out


def publish_move_one_joint_deg(joint_index: int, angle_deg: float) -> dict[str, Any]:
    if joint_index < 0 or joint_index > 6:
        return {"ok": False, "skipped": False, "reason": "joint_index must be 0..6"}
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": True, "skipped": True, "reason": "GO2_ENABLE_REAL_ARM off (dry)"}
    bad = _manual_dds_gate()
    if bad:
        return bad
    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) < 7:
        return {"ok": False, "skipped": False, "reason": "no_servo_feedback", "diag": diag}
    sd = [round(float(angles_fb[i]), 3) for i in range(7)]
    sd[joint_index] = round(float(angle_deg), 3)
    narrow = parse_step_deg_csv(os.environ.get("D1_ONE_JOINT_MAX_STEP_DEG"), editor_max_step_deg_7())
    try:
        dms = int(os.environ.get("D1_ONE_JOINT_DELAY_MS", os.environ.get("D1_EDITOR_MOVE_DELAY_MS", "420")))
    except ValueError:
        dms = 420
    return publish_goto_servo_deg7(sd, max_step_deg=narrow, delay_ms=dms, skip_prehold=False)


def save_true_zero_pose_json(
    *,
    servo_deg_override: list[float] | None = None,
    angle2_deg: float | None = None,
) -> dict[str, Any]:
    """Scrive ``data/true_zero_pose.json`` (posa ZERO) — richiede ``GO2_LOCAL``."""
    if os.environ.get("GO2_LOCAL", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "go2_local_off", "hint_it": "Salvataggio file sul robot: GO2_LOCAL=1."}
    arm: dict[str, Any]
    if servo_deg_override is not None and len(servo_deg_override) >= 6:
        sd = [round(float(servo_deg_override[i]), 3) for i in range(min(7, len(servo_deg_override)))]
        while len(sd) < 7:
            sd.append(sd[-1])
        joints_rad = [round(math.radians(sd[i]), 6) for i in range(6)]
        arm = {
            "feedback_ok": True,
            "servo_deg": sd,
            "joints_rad": joints_rad,
            "ik_seed_note": "servo_deg override from operator API — verify on hardware.",
        }
        if len(sd) >= 7:
            arm["gripper_deg"] = sd[6]
    else:
        cur, _diag = read_servo_deg_with_diag(PROJECT_ROOT)
        if cur is None or len(cur) < 6:
            return {
                "ok": False,
                "reason": "no_servo_feedback_for_true_zero",
                "hint": "Usa servo_deg nel JSON oppure verifica feedback DDS.",
            }
        sd = [round(float(cur[i]), 3) for i in range(min(7, len(cur)))]
        while len(sd) < 7:
            sd.append(sd[-1])
        if angle2_deg is not None:
            sd[2] = round(float(angle2_deg), 3)
        joints_rad = [round(math.radians(sd[i]), 6) for i in range(6)]
        arm = {
            "feedback_ok": True,
            "servo_deg": sd,
            "joints_rad": joints_rad,
            "ik_seed_note": "joints_rad: first 6 joints rad; servo_deg[6] gripper.",
        }
        if angle2_deg is not None:
            arm["angle2_deg_forced"] = round(float(angle2_deg), 3)
        if len(sd) >= 7:
            arm["gripper_deg"] = sd[6]
    payload: dict[str, Any] = {
        "label": "true_zero",
        "saved_at": _now_iso(),
        "note": (
            "Calibrated folded zero (operator / lite). Trajectory uses measured joint feedback between segments."
        ),
        "arm": arm,
    }
    TRUE_ZERO_POSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRUE_ZERO_POSE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "saved_to": str(TRUE_ZERO_POSE_PATH), "pose": payload}

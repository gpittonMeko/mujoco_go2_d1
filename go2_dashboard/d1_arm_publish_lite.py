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

from go2_dashboard import d1_arm_motion
from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag
from go2_dashboard.paths import PROJECT_ROOT
from go2_dashboard.sdk_backend import prefer_sdk_backend

# Stesso mount usato in ``operator_scene`` / ``openvla_runtime`` (base_link = arm + mount).
_MOUNT_BASE_LINK_M = (0.15, 0.0, 0.06)

# Snapshot ZERO e START su disco (stessi path della dashboard diagnostica completa).
TRUE_ZERO_POSE_PATH = PROJECT_ROOT / "data" / "true_zero_pose.json"
START_VARIANT_LATERAL = "lateral"
START_VARIANT_FRONTAL = "frontal"
START_VARIANTS = (START_VARIANT_LATERAL, START_VARIANT_FRONTAL)
LEGACY_ALIGNMENT_START_PATH = PROJECT_ROOT / "data" / "start_alignment.json"
START_ALIGNMENT_PATHS: dict[str, Path] = {
    START_VARIANT_LATERAL: PROJECT_ROOT / "data" / "start_alignment_lateral.json",
    START_VARIANT_FRONTAL: PROJECT_ROOT / "data" / "start_alignment_frontal.json",
}
# Default operativo: START laterale (90° — presa di lato).
ALIGNMENT_START_PATH = START_ALIGNMENT_PATHS[START_VARIANT_LATERAL]


def normalize_start_variant(variant: str | None) -> str:
    raw = (variant or os.environ.get("GO2_DEFAULT_START_VARIANT") or START_VARIANT_LATERAL).strip().lower()
    if raw in {"side", "lato", "laterale"}:
        return START_VARIANT_LATERAL
    if raw in {"front", "frontale"}:
        return START_VARIANT_FRONTAL
    return raw if raw in START_ALIGNMENT_PATHS else START_VARIANT_LATERAL


def resolve_start_alignment_path(variant: str | None = None) -> Path:
    """File START per variante; fallback su ``start_alignment.json`` legacy se il preset manca."""
    v = normalize_start_variant(variant)
    primary = START_ALIGNMENT_PATHS[v]
    if primary.is_file():
        return primary
    if LEGACY_ALIGNMENT_START_PATH.is_file():
        return LEGACY_ALIGNMENT_START_PATH
    return primary


def start_alignment_status() -> dict[str, Any]:
    return {
        "default_variant": START_VARIANT_LATERAL,
        "variants": {
            v: {
                "path": str(START_ALIGNMENT_PATHS[v]),
                "exists": START_ALIGNMENT_PATHS[v].is_file(),
                "resolved_path": str(resolve_start_alignment_path(v)),
                "resolved_exists": resolve_start_alignment_path(v).is_file(),
            }
            for v in START_VARIANTS
        },
        "legacy_path": str(LEGACY_ALIGNMENT_START_PATH),
        "legacy_exists": LEGACY_ALIGNMENT_START_PATH.is_file(),
    }

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
    return d1_arm_motion.motion_gate_error(require_plan_execute=True)


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


def _grasp_fast_align_enabled() -> bool:
    return os.environ.get("GO2_GRASP_FAST_START_ALIGN", "1").lower() in {"1", "true", "yes", "on"}


def _use_operator_arm_motion() -> bool:
    """Presa/fold/START usano lo stesso DDS del tab Giunti (session_begin + goto_deg)."""
    return os.environ.get("GO2_GRASP_USE_OPERATOR_ARM_MOTION", "1").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _file_pose_delay_ms(delay_ms: int | None, *, motion_profile: str = "default") -> int:
    if delay_ms is not None:
        return max(50, int(delay_ms))
    if (motion_profile or "").strip().lower() == "grasp_entry":
        for key in ("D1_GRASP_ENTRY_DELAY_MS", "D1_START_ALIGN_DELAY_MS", "D1_FOLD_DELAY_MS"):
            raw = (os.environ.get(key) or "").strip()
            if raw:
                try:
                    return max(50, int(raw))
                except ValueError:
                    break
        return 120
    v = (os.environ.get("D1_LITE_FILE_POSE_DELAY_MS") or os.environ.get("D1_EDITOR_MOVE_DELAY_MS") or "420").strip()
    try:
        return max(70, int(v))
    except ValueError:
        return 420


def _resolve_start_align_max_step_deg() -> list[float]:
    default_s = ",".join(str(x) for x in _DEFAULT_START_ALIGN_STEPS)
    if _grasp_fast_align_enabled():
        raw = (os.environ.get("D1_GRASP_START_ALIGN_MAX_STEP_DEG") or "").strip()
        if raw:
            return _parse_max_step_deg_7("D1_GRASP_START_ALIGN_MAX_STEP_DEG", default_s)
    return _parse_max_step_deg_7("D1_START_ALIGN_MAX_STEP_DEG", default_s)


def _resolve_zero_max_step_deg() -> list[float]:
    default_z = ",".join(str(x) for x in _DEFAULT_ZERO_TRANSITION_STEPS)
    if _grasp_fast_align_enabled():
        raw = (os.environ.get("D1_GRASP_ZERO_MAX_STEP_DEG") or "").strip()
        if raw:
            return _parse_max_step_deg_7("D1_GRASP_ZERO_MAX_STEP_DEG", default_z)
    return _parse_max_step_deg_7("D1_ZERO_TRANSITION_MAX_STEP_DEG", default_z)


def _grasp_phase_max_step_deg_7() -> list[float]:
    """Stessi passi del monolite ``_stage_messages(..., max_step_deg=D1_MAX_STEP_DEG_GRASP)``."""
    return _parse_max_step_deg_7("D1_MAX_STEP_DEG_GRASP", "1.5,0.8,1.2,2.0,2.0,2.5,4.0")


def _grasp_phase_delay_ms(delay_ms: int | None) -> int:
    if delay_ms is not None:
        return max(120, int(delay_ms))
    try:
        return max(120, int((os.environ.get("D1_PLAN_DELAY_MS") or "420").strip()))
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


def _pkill_d1_arm_command_processes() -> dict[str, Any]:
    """Termina publisher DDS (``d1_sdk_command`` / ``d1_arm_command``)."""
    return d1_arm_motion.kill_command_processes()


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
    if not d1_arm_motion.command_binary_ready():
        return {"ok": False, "reason": "missing_arm_command_bin", "backend": d1_arm_motion.motion_backend_name()}

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

    fc2_mode = _d1_fc2_multijoint_mode_trajectory()
    pub = d1_arm_motion.emergency_stop_hold(cur, repeats=rpt, delay_ms=dms, fc2_mode=fc2_mode)
    return {
        **pub,
        "feedback_diag": diag,
        "hold_repeats": rpt,
        "hold_delay_ms": dms,
        "warning_it": "E-stop software: verifica fisicamente braccio e zona; ripeti se necessario.",
        "backend": d1_arm_motion.motion_backend_name(),
    }


def _publish_servo_deg_trajectory(
    target7_deg: list[float],
    *,
    delay_ms: int | None,
    start7_deg: list[float] | None = None,
    feedback_diag: dict[str, Any] | None = None,
    max_step_deg: list[float] | None = None,
    motion_profile: str = "default",
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

    profile = (motion_profile or "default").strip().lower()
    if max_step_deg is not None and len(max_step_deg) >= 1:
        msd = list(max_step_deg[:7])
        while len(msd) < 7:
            msd.append(msd[-1])
        steps = msd
    elif profile == "grasp":
        steps = _grasp_phase_max_step_deg_7()
    elif profile == "grasp_entry":
        steps = _parse_max_step_deg_7("D1_GRASP_ENTRY_MAX_STEP_DEG", "6,3,2.8,4,6,6,10")
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
    if profile == "grasp_entry":
        try:
            step_scale = float(os.environ.get("D1_GRASP_ENTRY_TRAJ_STEP_SCALE", "1.0") or "1.0")
        except ValueError:
            step_scale = 1.0
    elif profile == "grasp":
        step_scale = max(step_scale, float(os.environ.get("D1_GRASP_TRAJ_STEP_SCALE_MIN", "0.45") or "0.45"))
    step_scale = max(0.12, min(1.0, step_scale))
    steps = [max(0.12, float(s) * step_scale) for s in steps]

    if prefer_sdk_backend():
        from go2_dashboard.d1_jog import service as jog_svc

        jog_step = min(steps) if steps else float(os.environ.get("D1_GRASP_JOINT_STEP_DEG", "1.8"))
        if profile == "grasp_entry":
            jog_step = max(steps)
        elif profile == "grasp":
            jog_step = min(steps)
        pub = jog_svc.move_servo_deg_jog_trajectory(tgt, max_step_deg=jog_step)
        return {
            **pub,
            "target_servo_deg_7": tgt,
            "feedback_diag": diag,
            "motion_profile": motion_profile,
        }

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
    messages = d1_arm_motion.build_fc2_trajectory_messages(path, fc2_mode=fc2_mode)
    if profile == "grasp":
        dms = _grasp_phase_delay_ms(delay_ms)
    elif profile == "grasp_entry":
        dms = _file_pose_delay_ms(delay_ms, motion_profile="grasp_entry")
    else:
        dms = max(120, int(delay_ms if delay_ms is not None else int(os.environ.get("GO2_OPENVLA_ARM_DELAY_MS", "500"))))
    pub = d1_arm_motion.publish_messages(messages, delay_ms=dms)
    return {
        **pub,
        "path_points": len(path),
        "target_servo_deg_7": tgt,
        "feedback_diag": diag,
        "backend": d1_arm_motion.motion_backend_name(),
        "motion_profile": motion_profile,
        "traj_delay_ms": dms,
    }


def goto_joints_rad_clamped_six(joints_rad: list[float], *, motion_profile: str = "default") -> dict[str, Any]:
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
    if not d1_arm_motion.command_binary_ready():
        return {"ok": False, "reason": "missing_arm_command_bin", "backend": d1_arm_motion.motion_backend_name()}

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
    if _use_operator_arm_motion():
        from go2_dashboard.operator_arm_motion import goto_servo_deg7_operator

        steps = _grasp_phase_max_step_deg_7() if motion_profile == "grasp" else editor_max_step_deg_7()
        out = goto_servo_deg7_operator(target7, max_step_deg=steps)
    else:
        out = _publish_servo_deg_trajectory(
            target7_deg=target7,
            delay_ms=None,
            start7_deg=[round(float(angles_fb[i]), 3) for i in range(7)],
            feedback_diag=diag,
            motion_profile=motion_profile,
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
    if _use_operator_arm_motion():
        from go2_dashboard.operator_arm_motion import goto_servo_deg7_operator_staged

        out = goto_servo_deg7_operator_staged(
            t7,
            max_step_deg=_resolve_zero_max_step_deg(),
            delay_ms=_file_pose_delay_ms(delay_ms, motion_profile="grasp_entry"),
        )
    else:
        profile = "grasp_entry" if _grasp_fast_align_enabled() else "default"
        dms = _file_pose_delay_ms(delay_ms, motion_profile=profile)
        steps = _resolve_zero_max_step_deg()
        out = _publish_servo_deg_trajectory(
            target7_deg=t7,
            delay_ms=dms,
            start7_deg=[round(float(angles_fb[i]), 3) for i in range(7)],
            feedback_diag=diag,
            max_step_deg=steps,
            motion_profile=profile,
        )
    out["mode"] = "goto_true_zero_file"
    out["true_zero_file"] = str(TRUE_ZERO_POSE_PATH)
    return out


def goto_saved_start_from_json(
    *,
    delay_ms: int | None = None,
    start_variant: str | None = None,
) -> dict[str, Any]:
    """Vai alla posa **START** salvata (``start_alignment_lateral.json`` default, o frontale)."""
    bad = _require_lite_arm_motion()
    if bad:
        return bad
    variant = normalize_start_variant(start_variant)
    start_path = resolve_start_alignment_path(variant)
    if not start_path.is_file():
        return {
            "ok": False,
            "reason": "no_start_alignment_json",
            "start_variant": variant,
            "path": str(start_path),
            "hint_it": (
                f"Salva START {variant} (tab Giunti / Teaching) — manca "
                f"data/start_alignment_{variant}.json."
            ),
        }
    try:
        data = json.loads(start_path.read_text(encoding="utf-8"))
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
    if _use_operator_arm_motion():
        from go2_dashboard.operator_arm_motion import goto_servo_deg7_operator_staged

        # START = transizione nota tra due pose valide: poche frazioni grosse (la suddivisione
        # interna per max_step_deg garantisce comunque la fluidità). Evita gli 8 sotto-passi lenti.
        start_fracs_raw = (os.environ.get("D1_START_ALIGN_PARTIAL_FRACTIONS") or "0.5,1.0").strip()
        try:
            start_fracs = [float(x.strip()) for x in start_fracs_raw.split(",") if x.strip()]
        except ValueError:
            start_fracs = [0.5, 1.0]
        out = goto_servo_deg7_operator_staged(
            t7,
            max_step_deg=_resolve_start_align_max_step_deg(),
            delay_ms=_file_pose_delay_ms(delay_ms, motion_profile="grasp_entry"),
            partial_fractions=start_fracs or [0.5, 1.0],
        )
    else:
        profile = "grasp_entry" if _grasp_fast_align_enabled() else "default"
        dms = _file_pose_delay_ms(delay_ms, motion_profile=profile)
        steps = _resolve_start_align_max_step_deg()
        out = _publish_servo_deg_trajectory(
            target7_deg=t7,
            delay_ms=dms,
            start7_deg=[round(float(angles_fb[i]), 3) for i in range(7)],
            feedback_diag=diag,
            max_step_deg=steps,
            motion_profile=profile,
        )
    out["mode"] = "goto_saved_start_file"
    out["start_variant"] = variant
    out["start_alignment_file"] = str(start_path)
    if isinstance(data.get("saved_at"), str):
        out["start_saved_at"] = data["saved_at"]
    return out


def _servo_delta_deg7(current: list[float], target: list[float]) -> list[float]:
    n = min(len(current), len(target), 7)
    return [round(float(current[i]) - float(target[i]), 3) for i in range(n)]


def save_start_alignment_json(
    *,
    servo_deg: list[float] | None = None,
    start_variant: str | None = None,
) -> dict[str, Any]:
    """Scrive preset START laterale (default) o frontale su ``data/``."""
    variant = normalize_start_variant(start_variant)
    start_path = START_ALIGNMENT_PATHS[variant]
    if os.environ.get("GO2_LOCAL", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "go2_local_off"}
    if isinstance(servo_deg, list) and len(servo_deg) >= 6:
        try:
            sd = [round(max(-135.0, min(135.0, float(servo_deg[i]))), 3) for i in range(min(7, len(servo_deg)))]
        except (TypeError, ValueError):
            return {"ok": False, "reason": "bad_servo_deg_list"}
        while len(sd) < 7:
            sd.append(sd[-1])
        source = "ui_servo_deg"
    else:
        angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
        if angles_fb is None or len(angles_fb) < 7:
            return {"ok": False, "reason": "no_servo_feedback", "diag": diag}
        sd = [round(float(angles_fb[i]), 3) for i in range(7)]
        source = "live_feedback"
    arm: dict[str, Any] = {
        "feedback_ok": True,
        "servo_deg": sd,
        "joints_rad": [round(math.radians(sd[i]), 6) for i in range(6)],
        "gripper_deg": sd[6],
        "ik_seed_note": f"START saved via dashboard lite ({source}).",
    }
    variant_label = "laterale" if variant == START_VARIANT_LATERAL else "frontale"
    payload = {
        "label": f"START {variant_label}",
        "start_variant": variant,
        "saved_at": datetime.now().astimezone().isoformat(),
        "note": f"Operational START {variant_label} — saved from operator dashboard.",
        "arm_at_start": arm,
    }
    try:
        start_path.parent.mkdir(parents=True, exist_ok=True)
        start_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        return {"ok": False, "reason": "start_alignment_write_failed", "detail": repr(exc)}
    return {
        "ok": True,
        "start_variant": variant,
        "saved_to": str(start_path),
        "servo_deg_7": sd,
        "start_pose": payload,
    }


def check_at_saved_start_pose(
    *,
    max_error_deg: float | None = None,
    start_variant: str | None = None,
) -> dict[str, Any]:
    """Confronta feedback servo con ``arm_at_start`` nel preset START scelto."""
    variant = normalize_start_variant(start_variant)
    start_path = resolve_start_alignment_path(variant)
    if not start_path.is_file():
        return {
            "ok": False,
            "reason": "no_start_alignment_json",
            "start_variant": variant,
            "hint_it": f"Salva START {variant} prima della presa.",
        }
    try:
        data = json.loads(start_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "reason": "start_alignment_read_failed", "detail": repr(exc)}
    t7 = _servo_deg7_from_arm_blob(data.get("arm_at_start") or {})
    if t7 is None:
        return {"ok": False, "reason": "saved_start_no_servo_angles"}
    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) < 7:
        return {"ok": False, "reason": "no_servo_feedback", "diag": diag}
    cur = [round(float(angles_fb[i]), 3) for i in range(7)]
    if max_error_deg is None:
        try:
            max_error_deg = float((os.environ.get("GO2_START_POSE_MAX_ERROR_DEG") or "6").strip())
        except ValueError:
            max_error_deg = 6.0
    deltas = _servo_delta_deg7(cur, t7)
    worst_i = max(range(len(deltas)), key=lambda i: abs(deltas[i])) if deltas else 0
    worst = abs(deltas[worst_i]) if deltas else 999.0
    ok = worst <= float(max_error_deg)
    return {
        "ok": ok,
        "at_saved_start": ok,
        "start_variant": variant,
        "start_alignment_file": str(start_path),
        "max_error_deg": round(worst, 3),
        "tolerance_deg": float(max_error_deg),
        "worst_joint": int(worst_i),
        "delta_deg_7": deltas,
        "current_servo_deg_7": cur,
        "target_servo_deg_7": t7,
        "hint_it": (
            "Braccio in posa START salvata."
            if ok
            else (
                f"Non in START: giunto {worst_i} err {worst:.1f}° (tol {max_error_deg}°). "
                "Esegui goto START o sequenza con confirm RUN_FULL_GRASP."
            )
        ),
    }


def goto_fold_compact_for_grasp() -> dict[str, Any]:
    """Fold compatto prima di START (come monolite ``_goto_fold_arm_pose``)."""
    if os.environ.get("GO2_GRASP_START_FOLD", "1").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": True, "skipped": True, "reason": "GO2_GRASP_START_FOLD_off"}
    bad = _require_lite_arm_motion()
    if bad:
        return bad
    s_scripts = str(PROJECT_ROOT / "scripts")
    if s_scripts not in sys.path:
        sys.path.insert(0, s_scripts)
    from arm_kinematics_d1_template import ARM_FOLD_POSE

    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) < 7:
        return {"ok": False, "reason": "no_servo_feedback", "diag": diag}
    q = [float(ARM_FOLD_POSE[i]) for i in range(min(6, len(ARM_FOLD_POSE)))]
    target_deg = [round(max(-135.0, min(135.0, math.degrees(q[i]))), 3) for i in range(6)]
    target7 = target_deg + [round(float(angles_fb[6]), 3)]
    steps = _parse_max_step_deg_7("D1_GRASP_FOLD_MAX_STEP_DEG", "4.5,2.3,2.1,3.0,4.5,4.8,8.0")
    if _use_operator_arm_motion():
        from go2_dashboard.operator_arm_motion import goto_servo_deg7_operator_staged

        out = goto_servo_deg7_operator_staged(
            target7,
            max_step_deg=steps,
            delay_ms=_file_pose_delay_ms(None, motion_profile="grasp_entry"),
        )
    else:
        profile = "grasp_entry" if _grasp_fast_align_enabled() else "grasp"
        out = _publish_servo_deg_trajectory(
            target7_deg=target7,
            delay_ms=_file_pose_delay_ms(None, motion_profile=profile),
            start7_deg=[round(float(angles_fb[i]), 3) for i in range(7)],
            feedback_diag=diag,
            max_step_deg=steps,
            motion_profile=profile,
        )
    out["mode"] = "goto_fold_compact"
    out["pose"] = "ARM_FOLD_POSE"
    return out


def goto_true_zero_then_saved_start_from_json(
    *,
    delay_ms: int | None = None,
    start_variant: str | None = None,
) -> dict[str, Any]:
    """Due traiettorie: ZERO file poi START file."""
    z = goto_true_zero_from_json(delay_ms=delay_ms)
    if not z.get("ok"):
        return {**z, "failed_segment": "true_zero"}
    s = goto_saved_start_from_json(delay_ms=delay_ms, start_variant=start_variant)
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


def _grasp_partial_fractions() -> list[float]:
    raw = (os.environ.get("GO2_GRASP_PARTIAL_FRACTIONS") or "0.12,0.24,0.38,0.52,0.68,0.84,1.0").strip()
    try:
        parts = [float(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        parts = [0.12, 0.24, 0.38, 0.52, 0.68, 0.84, 1.0]
    out = [max(0.05, min(1.0, p)) for p in parts]
    return sorted(set(out))


def goto_tool_target_base_link_m(
    xyz_base_link: list[float], *, delay_ms: int | None = None, motion_profile: str = "default"
) -> dict[str, Any]:
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
    if not d1_arm_motion.command_binary_ready():
        return {"ok": False, "reason": "missing_arm_command_bin", "backend": d1_arm_motion.motion_backend_name()}

    if len(xyz_base_link) < 3:
        return {"ok": False, "reason": "bad_xyz"}
    try:
        bl = [float(xyz_base_link[i]) for i in range(3)]
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad_xyz"}

    dz = float((os.environ.get("GO2_GRASP_IK_OFFSET_Z_BASE_LINK_M") or "0").strip() or "0.0")
    bl[2] += dz

    tip_arm = [bl[i] - float(_MOUNT_BASE_LINK_M[i]) for i in range(3)]

    if _use_operator_arm_motion():
        from go2_dashboard.operator_arm_motion import begin_operator_arm_session

        beg = begin_operator_arm_session()
        if not (beg.get("ok") or beg.get("skipped")):
            return {**beg, "mode": "ik_base_link"}
    elif prefer_sdk_backend():
        from go2_dashboard.d1_jog import service as jog_svc

        if jog_svc._arm_coupled:  # noqa: SLF001
            jog_svc.hold_pose_stream()
    elif os.environ.get("D1_GOTO_PREHOLD", "1").lower() in {"1", "true", "yes", "on"}:
        publish_d1_hold_current_lite(repeats=6)

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

    if _use_operator_arm_motion():
        from go2_dashboard.operator_arm_motion import goto_servo_deg7_operator

        steps = _grasp_phase_max_step_deg_7() if motion_profile == "grasp" else editor_max_step_deg_7()
        out = goto_servo_deg7_operator(target7, max_step_deg=steps, delay_ms=delay_ms)
    else:
        out = _publish_servo_deg_trajectory(
            target7_deg=target7,
            delay_ms=delay_ms,
            start7_deg=[round(float(angles_fb[i]), 3) for i in range(7)],
            feedback_diag=diag,
            motion_profile=motion_profile,
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
    # Approach all'oggetto: lento e a sotto-step parziali (mai un salto IK unico) verso il
    # waypoint, come richiesto. Disattivabile con GO2_GRASP_COACH_APPROACH_SUBSTEPS=0.
    substeps_on = os.environ.get("GO2_GRASP_COACH_APPROACH_SUBSTEPS", "1").lower() in {"1", "true", "yes", "on"}
    if substeps_on:
        try:
            approach_delay = int((os.environ.get("GO2_GRASP_COACH_APPROACH_DELAY_MS") or "").strip())
        except (TypeError, ValueError):
            approach_delay = delay_ms if delay_ms is not None else None
        inner = dict(
            goto_tool_target_base_link_m_staged(
                partial, delay_ms=approach_delay, motion_profile="grasp"
            )
        )
    else:
        inner = dict(goto_tool_target_base_link_m(partial, delay_ms=delay_ms))
    inner["mode"] = "ik_base_link_partial"
    inner["approach_blend_applied"] = ab
    inner["target_full_base_link_m"] = [round(tgt[i], 5) for i in range(3)]
    inner["waypoint_base_link_m"] = [round(partial[i], 5) for i in range(3)]
    inner["current_tip_base_link_m"] = cur
    return inner


def goto_tool_target_base_link_m_staged(
    xyz_base_link: list[float],
    *,
    delay_ms: int | None = None,
    motion_profile: str = "grasp",
    partial_fractions: list[float] | None = None,
) -> dict[str, Any]:
    """Progressioni parziali in ``base_link`` (come coach/jog) — mai un salto IK unico."""
    cur, diag = current_tool_tip_base_link_m()
    if cur is None:
        return {"ok": False, "reason": "no_current_tip_fk", "diag": diag}
    try:
        tgt = [float(xyz_base_link[i]) for i in range(3)]
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad_xyz"}
    fracs = partial_fractions if partial_fractions else _grasp_partial_fractions()
    partial_steps: list[dict[str, Any]] = []
    ok_all = True
    for frac in fracs:
        wp = [cur[i] + float(frac) * (tgt[i] - cur[i]) for i in range(3)]
        r = goto_tool_target_base_link_m(wp, delay_ms=delay_ms, motion_profile=motion_profile)
        r["partial_fraction"] = float(frac)
        r["waypoint_base_link_m"] = [round(x, 5) for x in wp]
        partial_steps.append(r)
        if not r.get("ok"):
            ok_all = False
            break
        if prefer_sdk_backend():
            from go2_dashboard.d1_jog import service as jog_svc

            jog_svc.hold_pose_stream()
    return {
        "ok": ok_all,
        "mode": "ik_base_link_staged",
        "partial_count": len(partial_steps),
        "partial_steps": partial_steps,
        "target_full_base_link_m": [round(tgt[i], 5) for i in range(3)],
        "current_tip_base_link_m": cur,
    }


def goto_joints_rad_clamped_six_staged(
    joints_rad: list[float],
    *,
    motion_profile: str = "grasp",
    partial_fractions: list[float] | None = None,
) -> dict[str, Any]:
    """Progressioni parziali in spazio giunti verso ``joints_rad`` (micro-step jog)."""
    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) < 7:
        return {"ok": False, "reason": "no_servo_feedback", "diag": diag}
    s_scripts = str(PROJECT_ROOT / "scripts")
    if s_scripts not in sys.path:
        sys.path.insert(0, s_scripts)
    from arm_kinematics_d1_template import J_LIMITS, clamp

    if len(joints_rad) < 6:
        return {"ok": False, "reason": "need_6_joint_radians"}
    tgt_q = [clamp(float(joints_rad[i]), *J_LIMITS[i]) for i in range(6)]
    cur_q = [math.radians(float(angles_fb[i])) for i in range(6)]
    fracs = partial_fractions if partial_fractions else _grasp_partial_fractions()
    partial_steps: list[dict[str, Any]] = []
    ok_all = True
    for frac in fracs:
        q = [cur_q[i] + float(frac) * (tgt_q[i] - cur_q[i]) for i in range(6)]
        r = goto_joints_rad_clamped_six(q, motion_profile=motion_profile)
        r["partial_fraction"] = float(frac)
        partial_steps.append(r)
        if not r.get("ok"):
            ok_all = False
            break
        if prefer_sdk_backend():
            from go2_dashboard.d1_jog import service as jog_svc

            jog_svc.hold_pose_stream()
    return {
        "ok": ok_all,
        "mode": "joints_rad_staged",
        "partial_count": len(partial_steps),
        "partial_steps": partial_steps,
        "target_joints_rad_6": [round(float(x), 5) for x in tgt_q],
    }


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
    gate = d1_arm_motion.motion_gate_error()
    if gate:
        gate["skipped"] = False
        return gate
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
    if prefer_sdk_backend():
        if _use_operator_arm_motion():
            from go2_dashboard.operator_arm_motion import hold_operator_arm_pose

            holds = [hold_operator_arm_pose() for _ in range(max(1, int(rpt)))]
            ok = all(h.get("ok") or h.get("skipped") for h in holds)
            return {
                "ok": ok,
                "mode": "hold_current",
                "stream": True,
                "holds": holds,
                "snapshot_deg": cur,
                "motion_path": "operator_ui_hold",
            }
        from go2_dashboard.d1_jog import service as jog_svc

        if not jog_svc._arm_coupled:  # noqa: SLF001
            wp = os.environ.get("GO2_GRASP_ARM_WITH_POWER", "0").lower() in {"1", "true", "yes", "on"}
            prep = jog_svc.arm_motion_session_begin(with_power=wp)
            if not prep.get("ok"):
                return {**prep, "mode": "hold_current"}
        holds: list[dict[str, Any]] = []
        for _ in range(max(1, int(rpt))):
            holds.append(jog_svc.hold_pose_stream(servo_deg=cur))
        return {"ok": True, "mode": "hold_current", "stream": True, "holds": holds, "snapshot_deg": cur}
    fc2_mode = _d1_fc2_multijoint_mode_trajectory()
    path = [cur] * max(1, int(rpt))
    msgs = d1_arm_motion.build_fc2_trajectory_messages(path, fc2_mode=fc2_mode, include_couple=True)
    pub = d1_arm_motion.publish_messages(msgs, delay_ms=int(dms))
    pub["mode"] = "hold_current"
    return pub


def publish_live_pose_deg7(servo_deg: list[float]) -> dict[str, Any]:
    """Slider live: con SDK usa daemon DDS persistente (come dashboard jog 5053)."""
    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": True, "skipped": True, "reason": "GO2_ENABLE_REAL_ARM off (dry)"}
    bad = _manual_dds_gate()
    if bad:
        return bad
    pre_meta: dict[str, Any] = {}
    if (
        not prefer_sdk_backend()
        and os.environ.get("D1_LIVE_PREHOLD", "1").lower() in {"1", "true", "yes", "on"}
    ):
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
    out = d1_arm_motion.publish_live_servo_deg7(servo_deg)
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
        and not d1_arm_motion.is_live_session_active()
        and os.environ.get("D1_GOTO_PREHOLD", "1").lower() in {"1", "true", "yes"}
    ):
        publish_d1_hold_current_lite(
            repeats=max(6, int(os.environ.get("D1_GOTO_PREHOLD_REPEATS", "14"))),
            delay_ms=max(35, int(os.environ.get("D1_GOTO_PREHOLD_DELAY_MS", "55"))),
        )

    if prefer_sdk_backend() and not d1_arm_motion.is_live_session_active() and _use_operator_arm_motion():
        from go2_dashboard.operator_arm_motion import begin_operator_arm_session

        beg = begin_operator_arm_session()
        if not (beg.get("ok") or beg.get("skipped")):
            return beg

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

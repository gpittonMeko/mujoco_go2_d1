from __future__ import annotations

import copy
import math
import os
import re
from typing import Any

from go2_dashboard.d1_arm_publish_lite import (
    arm_emergency_stop_hold,
    goto_home_servo_deg,
    goto_saved_start_from_json,
    goto_true_zero_from_json,
    goto_true_zero_then_saved_start_from_json,
    goto_tool_target_base_link_m_partial,
    publish_d1_hold_current_lite,
    publish_move_one_joint_deg,
)
from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag
from go2_dashboard.operator_session_memory import (
    append_operator_session_event,
    read_recent_operator_session_events,
)
from go2_dashboard.paths import PROJECT_ROOT
from go2_dashboard.sport_lane import accompany_execute_json


def _hermes_step_failed(step: dict[str, Any]) -> bool:
    hs = step.get("http_status")
    if hs is not None:
        try:
            if int(hs) >= 400:
                return True
        except (TypeError, ValueError):
            return True
    res = step.get("result")
    if isinstance(res, dict) and "ok" in res and res.get("ok") is False:
        return True
    return False


def _hermes_run_arm_preset(preset: str) -> dict[str, Any]:
    p = preset.strip().lower()
    if p == "home":
        return dict(goto_home_servo_deg(delay_ms=None))
    if p == "true_zero":
        return dict(goto_true_zero_from_json(delay_ms=None))
    if p == "saved_start":
        return dict(goto_saved_start_from_json(delay_ms=None))
    if p == "zero_then_start":
        return dict(goto_true_zero_then_saved_start_from_json(delay_ms=None))
    if p == "estop":
        return dict(arm_emergency_stop_hold())
    return {"ok": False, "reason": "unknown_arm_preset", "preset": preset}


def _hermes_run_arm_joint_delta(joint_index: int, delta_deg: float) -> dict[str, Any]:
    """Piccolo spostamento relativo rispetto ai servo_deg correnti (stesso stack di ``/api/arm/joints/move_one``)."""
    if joint_index < 0 or joint_index > 6:
        return {"ok": False, "reason": "joint_index_out_of_range"}
    try:
        mx = float((os.environ.get("GO2_HERMES_ARM_NUDGE_MAX_DELTA_DEG") or "45").strip())
    except ValueError:
        mx = 45.0
    mx = max(2.0, min(mx, 45.0))
    try:
        d = float(delta_deg)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad_delta_deg"}
    if abs(d) > mx:
        d = mx if d > 0 else -mx
    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) <= joint_index:
        return {"ok": False, "reason": "no_servo_feedback", "diag": diag}
    target = round(float(angles_fb[joint_index]) + d, 3)
    return dict(publish_move_one_joint_deg(joint_index, target))


def _hermes_intent_has_arm_motion(intent: dict[str, Any]) -> bool:
    ap = intent.get("arm_preset")
    if isinstance(ap, str) and ap.strip():
        return True
    aj = intent.get("arm_joint_delta")
    if isinstance(aj, dict):
        raw_ji = aj.get("joint_index")
        try:
            ji = int(raw_ji) if raw_ji is not None else -1
        except (TypeError, ValueError):
            ji = -1
        if ji >= 0:
            return True
    att = intent.get("arm_tool_target")
    if isinstance(att, dict) and _hermes_sanitize_tool_target_xyz(att.get("xyz_base_link_m")):
        return True
    return False


def _hermes_intent_arm_joint_delta_only(intent: dict[str, Any]) -> bool:
    """Solo jog relativo: ``publish_move_one_joint_deg`` ha già pre-hold interno."""
    if not _hermes_intent_has_arm_motion(intent):
        return False
    ap = intent.get("arm_preset")
    if isinstance(ap, str) and ap.strip():
        return False
    att = intent.get("arm_tool_target")
    if isinstance(att, dict) and _hermes_sanitize_tool_target_xyz(att.get("xyz_base_link_m")):
        return False
    aj = intent.get("arm_joint_delta")
    if not isinstance(aj, dict):
        return False
    raw_ji = aj.get("joint_index")
    try:
        ji = int(raw_ji) if raw_ji is not None else -1
    except (TypeError, ValueError):
        ji = -1
    return ji >= 0


def _hermes_intent_arm_tool_target_only(intent: dict[str, Any]) -> bool:
    """Solo IK visione: ``goto_tool_target_base_link_m`` ora fa ``publish_d1_hold_current_lite`` prima dell'IK."""
    if not _hermes_intent_has_arm_motion(intent):
        return False
    ap = intent.get("arm_preset")
    if isinstance(ap, str) and ap.strip():
        return False
    aj = intent.get("arm_joint_delta")
    if isinstance(aj, dict):
        raw_ji = aj.get("joint_index")
        try:
            ji = int(raw_ji) if raw_ji is not None else -1
        except (TypeError, ValueError):
            ji = -1
        if ji >= 0:
            return False
    att = intent.get("arm_tool_target")
    return isinstance(att, dict) and _hermes_sanitize_tool_target_xyz(att.get("xyz_base_link_m")) is not None


def _hermes_append_pre_move_hold(steps: list[dict[str, Any]], intent: dict[str, Any]) -> None:
    """Raffica hold sulla posa corrente prima della prima azione braccio (preset / IK / catena)."""
    if os.environ.get("GO2_HERMES_PRE_MOVE_HOLD", "1").lower() not in {"1", "true", "yes", "on"}:
        return
    if not _hermes_intent_has_arm_motion(intent):
        return
    if _hermes_intent_arm_joint_delta_only(intent) and os.environ.get(
        "GO2_HERMES_PRE_MOVE_HOLD_SKIP_JOINT_ONLY", "1"
    ).lower() in {"1", "true", "yes", "on"}:
        return
    if _hermes_intent_arm_tool_target_only(intent) and os.environ.get(
        "GO2_HERMES_PRE_MOVE_HOLD_SKIP_TOOL_ONLY", "1"
    ).lower() in {"1", "true", "yes", "on"}:
        return
    rpt_env = (os.environ.get("GO2_HERMES_PRE_MOVE_HOLD_REPEATS") or "").strip()
    dms_env = (os.environ.get("GO2_HERMES_PRE_MOVE_HOLD_DELAY_MS") or "").strip()
    try:
        rpt = int(rpt_env) if rpt_env else None
    except ValueError:
        rpt = None
    try:
        dms = int(dms_env) if dms_env else None
    except ValueError:
        dms = None
    r = dict(publish_d1_hold_current_lite(repeats=rpt, delay_ms=dms))
    steps.append({"kind": "hermes_pre_move_hold", "result": r})


def _hermes_apply_intent(intent: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    stop_on_err = os.environ.get("GO2_HERMES_STOP_ON_ERROR", "1").lower() in {"1", "true", "yes", "on"}

    if dry_run:
        return {"ok": True, "dry_run": True, "steps": steps, "intent": intent}

    _hermes_append_pre_move_hold(steps, intent)
    if steps and stop_on_err and _hermes_step_failed(steps[-1]):
        return {"ok": False, "dry_run": False, "steps": steps, "intent": intent}

    ap = intent.get("arm_preset")
    if isinstance(ap, str) and ap.strip():
        r = _hermes_run_arm_preset(ap)
        step = {"kind": "arm_preset", "preset": ap.strip(), "result": r}
        steps.append(step)
        if stop_on_err and _hermes_step_failed(step):
            return {"ok": False, "dry_run": False, "steps": steps, "intent": intent}

    aj = intent.get("arm_joint_delta")
    if isinstance(aj, dict):
        raw_ji = aj.get("joint_index")
        raw_dd = aj.get("delta_deg")
        try:
            ji = int(raw_ji) if raw_ji is not None else -1
            dd = float(raw_dd) if raw_dd is not None else 0.0
        except (TypeError, ValueError):
            ji = -1
            dd = 0.0
        if ji >= 0:
            r = _hermes_run_arm_joint_delta(ji, dd)
            step = {"kind": "arm_joint_delta", "joint_index": ji, "delta_deg_requested": dd, "result": r}
            steps.append(step)
            if stop_on_err and _hermes_step_failed(step):
                return {"ok": False, "dry_run": False, "steps": steps, "intent": intent}

    att = intent.get("arm_tool_target")
    if isinstance(att, dict):
        raw_xyz = att.get("xyz_base_link_m")
        xyz = _hermes_sanitize_tool_target_xyz(raw_xyz)
        if xyz is not None:
            blend = _hermes_clamp_approach_blend(att.get("approach_blend"))
            r = goto_tool_target_base_link_m_partial(xyz, approach_blend=blend, delay_ms=None)
            step = {
                "kind": "arm_tool_target_partial",
                "xyz_base_link_m": xyz,
                "approach_blend": blend,
                "result": r,
            }
            steps.append(step)
            if stop_on_err and _hermes_step_failed(step):
                return {"ok": False, "dry_run": False, "steps": steps, "intent": intent}

    bm = intent.get("base_motion")
    if isinstance(bm, dict) and bm.get("mode"):
        body = dict(bm)
        body.setdefault("sync", True)
        payload, code = accompany_execute_json(body, query_sync_flag=False)
        step = {"kind": "base_motion", "http_status": code, "result": payload}
        steps.append(step)
        if stop_on_err and _hermes_step_failed(step):
            return {"ok": False, "dry_run": False, "steps": steps, "intent": intent}

    ok_all = True if steps else True
    for s in steps:
        if _hermes_step_failed(s):
            ok_all = False
            break

    return {"ok": ok_all, "dry_run": False, "steps": steps, "intent": intent}


def _hermes_allow_arm_joint_delta(caps: dict[str, Any]) -> bool:
    if caps.get("_legacy"):
        return True
    return bool(caps.get("allow_arm_joint_delta"))


def _hermes_allow_arm_tool_target(caps: dict[str, Any]) -> bool:
    if caps.get("_legacy"):
        return True
    return bool(caps.get("allow_arm_tool_target"))


def _hermes_sanitize_tool_target_xyz(raw: Any) -> list[float] | None:
    """Stesso sandbox numerico di Grasp Coach (base_link m)."""
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        return None
    try:
        x, y, z = float(raw[0]), float(raw[1]), float(raw[2])
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, z)):
        return None
    if abs(x) > 1.2 or abs(y) > 0.9 or abs(z) > 1.1:
        return None
    return [x, y, z]


def _hermes_clamp_approach_blend(raw: Any) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = 0.22
    try:
        mx = float(os.environ.get("GO2_GRASP_COACH_MAX_APPROACH_BLEND", "0.28") or "0.28")
    except ValueError:
        mx = 0.28
    mx = max(0.08, min(mx, 0.42))
    return max(0.06, min(mx, v))


_ARM_TERMS_RE = re.compile(r"(?i)\b(arm|braccio)\b")
_DEG_AMOUNT_RE = re.compile(r"(?i)(?:^|[^\d])(\d+(?:\.\d+)?)\s*(?:°|deg|gradi|degrees)\b")
# Manipulation vocabulary: if present, do not map «alza il cane»-style phrases away from arm intent.
_MANIP_TERMS_RE = re.compile(
    r"(?i)\b(arm|arms|braccio|braccia|giunto|joint|joints|pinza|gripper|manipol|tool|wrist|polso|d1)\b"
)
# Go2 quadruped «dog» base — Sport stand / crouch / stop (Italian + short English).
_GO2_BASE_STAND_RE = re.compile(
    r"(?i)(\balza(il)?\s+il\s+cane\b|\balza(il)?\s+il\s+go2\b|\balza(il)?\s+il\s+quadrupede\b|\bmetti(il)?\s+il\s+cane\s+in\s+piedi\b|\bil\s+cane\s+in\s+piedi\b|\brialza(il)?\s+il\s+cane\b|\bstand\s+up\b(?:\s+(?:the\s+)?(?:dog|robot|go2|unitree))?|\bwake\s+(?:the\s+)?(?:dog|go2|robot)\b)"
)
_GO2_BASE_CROUCH_RE = re.compile(
    r"(?i)(\baccuccia(il)?\s+il\s+cane\b|\babbassa(il)?\s+il\s+cane\b|\bmetti(il)?\s+il\s+cane\s+a\s+terra\b|\bil\s+cane\s+a\s+terra\b|\bcrouch\b(?:\s+(?:the\s+)?(?:dog|robot|go2))?|\blie\s+down\b(?:\s+(?:the\s+)?(?:dog|robot|go2))?)"
)
_GO2_BASE_STOP_RE = re.compile(
    r"(?i)(\bferma(il)?\s+il\s+cane\b|\bferma\s+la\s+base\b|\bstop\b\s+(?:la\s+)?(?:base|sport(?:\s+mode)?)|\bferma\s+il\s+go2\b)"
)

_HERMES_DEFAULT_BASE_MOTION: dict[str, Any] = {
    "mode": "stand_up",
    "enable": True,
    "stand_up_first": False,
    "speed_level": None,
    "sync": True,
    "vx": None,
    "vy": None,
    "vyaw": None,
    "pre_balance": True,
}


def hermes_apply_go2_base_lexicon_from_user_text(
    user_text: str, intent: dict[str, Any], caps: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Se il testo parla chiaramente della base Go2 (Sport) e non di manipolazione, correggi l'intent LLM.

    Evita che frasi tipo «alza il cane» finiscano per errore in ``arm_joint_delta`` per alone del turno precedente.
    """
    notes: list[str] = []
    if os.environ.get("GO2_HERMES_BASE_LEXICON", "1").lower() not in {"1", "true", "yes", "on"}:
        return intent, notes
    if not caps.get("allow_base_motion") and not caps.get("_legacy"):
        return intent, notes
    raw = (user_text or "").strip()
    if len(raw) > 600:
        raw = raw[:600]
    if _MANIP_TERMS_RE.search(raw):
        return intent, notes

    mode_pick: str | None = None
    if _GO2_BASE_STOP_RE.search(raw):
        mode_pick = "stop"
    elif _GO2_BASE_CROUCH_RE.search(raw):
        mode_pick = "crouch"
    elif _GO2_BASE_STAND_RE.search(raw):
        mode_pick = "stand_up"

    if mode_pick is None:
        return intent, notes

    out = copy.deepcopy(intent)
    out["arm_preset"] = None
    out["arm_joint_delta"] = None
    out["arm_tool_target"] = None

    bm = dict(_HERMES_DEFAULT_BASE_MOTION)
    bm["mode"] = mode_pick
    out["base_motion"] = bm

    label = {"stand_up": "stand_up (Go2 Sport)", "crouch": "crouch (Go2 Sport)", "stop": "stop (Sport)"}.get(
        mode_pick, mode_pick
    )
    notes.append(f"Routing server: frase operatore interpretata come base quadrupede → `{label}`; campi braccio azzerati.")
    return out, notes


def hermes_inject_arm_joint_delta_from_user_text(
    user_text: str, intent: dict[str, Any], caps: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Se il modello rifiuta il jog, prova a ricavare ``arm_joint_delta`` dal testo operatore."""
    notes: list[str] = []
    if not _hermes_allow_arm_joint_delta(caps):
        return intent, notes

    aj = intent.get("arm_joint_delta")
    if isinstance(aj, dict) and aj.get("joint_index") is not None and aj.get("delta_deg") is not None:
        return intent, notes

    ap = intent.get("arm_preset")
    if isinstance(ap, str) and ap.strip():
        return intent, notes

    if isinstance(intent.get("arm_tool_target"), dict) and _hermes_sanitize_tool_target_xyz(
        intent["arm_tool_target"].get("xyz_base_link_m")  # type: ignore[index]
    ):
        return intent, notes

    raw = (user_text or "").strip()
    if len(raw) > 500:
        raw = raw[:500]
    if not _ARM_TERMS_RE.search(raw):
        return intent, notes

    dm = _DEG_AMOUNT_RE.search(raw)
    if not dm:
        return intent, notes
    try:
        deg = float(dm.group(1))
    except (TypeError, ValueError):
        return intent, notes
    if deg <= 0 or deg > 120:
        return intent, notes

    low = raw.lower()
    ji = 0
    sign = -1.0
    if re.search(r"\b(sinistra|left)\b", low):
        ji, sign = 0, -1.0
    elif re.search(r"\b(destra|right)\b", low):
        ji, sign = 0, 1.0
    elif re.search(r"\b(avanti|forward|ahead)\b", low):
        ji, sign = 1, 1.0
    elif re.search(r"\b(indietro|back|backward)\b", low):
        ji, sign = 1, -1.0
    elif re.search(r"\b(su|up)\b", low) and "volume" not in low:
        ji, sign = 2, 1.0
    elif re.search(r"\b(gi[uù]|down|basso)\b", low):
        ji, sign = 2, -1.0
    else:
        return intent, notes

    out = copy.deepcopy(intent)
    out["arm_joint_delta"] = {"joint_index": ji, "delta_deg": sign * deg}
    notes.append(
        "Jog braccio da regola server (testo + gradi): il modello non aveva emesso `arm_joint_delta` corretto."
    )
    return out, notes


def _hermes_capabilities_from_body(body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("capabilities")
    if not isinstance(raw, dict):
        return {
            "_legacy": True,
            "allow_base_motion": True,
            "allow_base_stand_crouch": True,
            "allow_base_joystick": True,
            "allow_base_velocity": True,
            "allow_base_damping": True,
            "allow_arm_presets": True,
            "allow_arm_joint_delta": True,
            "allow_arm_tool_target": True,
            "allow_openvla_plan": False,
            "allow_openvla_execute": False,
            "lab_fragile_payload": False,
        }
    return {
        "_legacy": False,
        "allow_base_motion": bool(raw.get("allow_base_motion")),
        "allow_base_stand_crouch": bool(raw.get("allow_base_stand_crouch")),
        "allow_base_joystick": bool(raw.get("allow_base_joystick")),
        "allow_base_velocity": bool(raw.get("allow_base_velocity")),
        "allow_base_damping": bool(raw.get("allow_base_damping")),
        "allow_arm_presets": bool(raw.get("allow_arm_presets")),
        "allow_arm_joint_delta": bool(raw.get("allow_arm_joint_delta", raw.get("allow_arm_presets"))),
        "allow_arm_tool_target": bool(raw.get("allow_arm_tool_target", raw.get("allow_arm_presets"))),
        "allow_openvla_plan": bool(raw.get("allow_openvla_plan")),
        "allow_openvla_execute": bool(raw.get("allow_openvla_execute")),
        "lab_fragile_payload": bool(raw.get("lab_fragile_payload")),
    }


def _hermes_routing_note_for_caps(caps: dict[str, Any]) -> str:
    if caps.get("_legacy"):
        return ""
    chunks: list[str] = [
        "Operator capability profile from UI: if the verbal request exceeds enabled caps, explain in "
        "`assistant_reply_it` (English) and leave action fields null/false accordingly.",
        "**damping** and **velocity** Sport modes require explicit UI toggles `allow_base_damping` and "
        "`allow_base_velocity`. If disabled (default), never emit those modes — use stop/balance_hold instead.",
    ]
    if caps.get("lab_fragile_payload"):
        chunks.append(
            "FRAGILE payload mounted high on the back: prefer `base_motion` null or only `stop` / `balance_hold` "
            "when necessary — avoid stand_up, crouch, velocity, damping, recovery_stand, joystick "
            "because they tilt or disturb the base."
        )
    if not caps.get("allow_base_motion"):
        chunks.append("`base_motion` must be null (base disabled by UI).")
    elif not caps.get("allow_base_stand_crouch"):
        chunks.append("Do not use modes `stand_up` or `crouch` in JSON.")
    elif caps.get("allow_base_stand_crouch"):
        chunks.append(
            "**Italian:** «alza il cane» / «metti il cane in piedi» ⇒ **`base_motion.mode` = `stand_up`** (Go2 dog), "
            "not `arm_joint_delta`. «abbassa / accuccia il cane» ⇒ **`crouch`**."
        )
    if not caps.get("allow_base_joystick"):
        chunks.append("Do not use mode `joystick`.")
    if not caps.get("allow_base_velocity"):
        chunks.append("Do not use mode `velocity` (blocked by UI unless explicitly enabled).")
    if not caps.get("allow_base_damping"):
        chunks.append("Do not use mode `damping` (blocked by UI unless explicitly enabled).")
    if not caps.get("allow_arm_presets"):
        chunks.append("`arm_preset` always null (arm presets disabled by UI).")
    if _hermes_allow_arm_joint_delta(caps) or caps.get("_legacy"):
        chunks.append(
            "**CRITICAL arm jog**: Relative moves use **only** `arm_joint_delta`. "
            "There is **no** OpenVLA and **no** «full plan» for these requests — "
            "refusing or mentioning «full plan» / «need a plan» for degree-sized arm nudges is **wrong**. "
            "Emit `arm_joint_delta` with `joint_index` and `delta_deg` (Italian left/sinistra ⇒ joint 0, negative deg)."
        )
    if not _hermes_allow_arm_joint_delta(caps) and not caps.get("_legacy"):
        chunks.append("`arm_joint_delta` must be null (small arm jog disabled by UI).")
    if _hermes_allow_arm_tool_target(caps) or caps.get("_legacy"):
        chunks.append(
            "**Vision reach (no OpenVLA):** when camera frames are attached and the operator asks to move the arm "
            "toward a visible object, estimate a conservative grasp/pre-grasp point in **`base_link`** (x forward, "
            "y left, z up from the robot) and emit **`arm_tool_target`** with `xyz_base_link_m` and "
            "`approach_blend` ∈ [0.12, 0.28]. The server runs **partial IK** toward that point (same idea as "
            "Grasp Coach). Iterate in chat for finer alignment."
        )
    if not _hermes_allow_arm_tool_target(caps) and not caps.get("_legacy"):
        chunks.append("`arm_tool_target` must be null (vision IK disabled by UI).")
    chunks.append(
        "Legacy field `openvla` is **not** used — omit it from JSON (server strips it if present)."
    )
    return "\n".join(chunks)


def _hermes_sanitize_intent(intent: dict[str, Any], caps: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    out = copy.deepcopy(intent)
    warnings: list[str] = []

    bm = out.get("base_motion")
    if isinstance(bm, dict) and bm.get("mode"):
        mode = str(bm.get("mode") or "").strip().lower()
        fragile = bool(caps.get("lab_fragile_payload")) and not caps.get("_legacy")
        kill = False

        if not caps.get("allow_base_motion"):
            kill = True
            warnings.append("Base motion disabled by operator — action stripped.")
        elif mode == "velocity" and not caps.get("allow_base_velocity"):
            kill = True
            warnings.append("Sport mode `velocity` disabled — use stand/crouch/stop/balance unless UI enables it.")
        elif mode == "damping" and not caps.get("allow_base_damping"):
            kill = True
            warnings.append("Sport mode `damping` disabled — do not auto-insert damping.")
        elif fragile and mode in {"velocity", "damping", "recovery_stand"}:
            kill = True
            warnings.append(f"Mode `{mode}` blocked with fragile payload on back — action stripped.")
        elif fragile and mode in {"stand_up", "crouch"} and not caps.get("allow_base_stand_crouch"):
            kill = True
            warnings.append("Stand/crouch not enabled (fragile or UI) — action stripped.")
        elif fragile and mode == "joystick" and not caps.get("allow_base_joystick"):
            kill = True
            warnings.append("Joystick not enabled for this safety profile — action stripped.")
        elif not fragile:
            if mode in {"stand_up", "crouch"} and not caps.get("allow_base_stand_crouch"):
                kill = True
                warnings.append("Stand/crouch disabled by operator — action stripped.")
            elif mode == "joystick" and not caps.get("allow_base_joystick"):
                kill = True
                warnings.append("Joystick disabled by operator — action stripped.")

        if kill:
            out["base_motion"] = None

    if out.get("openvla") is not None:
        out["openvla"] = None
        warnings.append("Campo legacy `openvla` ignorato (stack vision → IK locale).")

    ap = out.get("arm_preset")
    if isinstance(ap, str) and ap.strip():
        if not caps.get("allow_arm_presets"):
            out["arm_preset"] = None
            warnings.append("Hermes arm presets disabled by UI (`arm_preset` cleared).")

    att = out.get("arm_tool_target")
    if att is not None:
        if not _hermes_allow_arm_tool_target(caps):
            out["arm_tool_target"] = None
            warnings.append("Hermes vision IK disabled by UI (`arm_tool_target` cleared).")
        elif isinstance(att, dict):
            xyz = _hermes_sanitize_tool_target_xyz(att.get("xyz_base_link_m"))
            if xyz is None:
                out["arm_tool_target"] = None
                warnings.append("`arm_tool_target` invalid xyz — dropped.")
            else:
                blend = _hermes_clamp_approach_blend(att.get("approach_blend"))
                out["arm_tool_target"] = {"xyz_base_link_m": xyz, "approach_blend": blend}
        else:
            out["arm_tool_target"] = None
            warnings.append("`arm_tool_target` must be object — dropped.")

    ajd = out.get("arm_joint_delta")
    if ajd is not None:
        if out.get("arm_tool_target"):
            out["arm_joint_delta"] = None
            warnings.append("`arm_joint_delta` cleared (`arm_tool_target` takes precedence this turn).")
        elif not _hermes_allow_arm_joint_delta(caps):
            out["arm_joint_delta"] = None
            warnings.append("Hermes small arm jog disabled by UI (`arm_joint_delta` cleared).")
        elif isinstance(ajd, dict):
            try:
                ji = int(ajd.get("joint_index"))
                dd = float(ajd.get("delta_deg"))
                if ji < 0 or ji > 6:
                    raise ValueError("joint_index range")
                out["arm_joint_delta"] = {"joint_index": ji, "delta_deg": dd}
            except (TypeError, ValueError):
                out["arm_joint_delta"] = None
                warnings.append("`arm_joint_delta` invalid — dropped.")

    return out, warnings


def hermes_try_play_tts_mp3_on_go2_webrtc(b64_mp3: str) -> bool:
    """Fallback WebRTC (RoboVerse / ``unitree_webrtc_connect``): MP3 → track audio sul peer come da esempio upstream."""
    from go2_dashboard.go2_voice_webrtc import try_play_b64_mp3_via_webrtc_subprocess

    return try_play_b64_mp3_via_webrtc_subprocess(b64_mp3)


def hermes_try_play_tts_mp3_on_go2_speaker(b64_mp3: str) -> bool:
    """Riproduce TTS sul **Go2** via RPC DDS ``voice`` (SDK) se ``GO2_HERMES_PLAY_ON_GO2=1``.

    Decodifica MP3→PCM con ``ffmpeg``, poi ``AudioClient.PlayStream`` come negli esempi G1 del repo.
    Serve ``GO2_LOCAL=1``, ``GO2_DDS_*`` coerenti col cane e firmware che espone il servizio ``voice``.
    """
    from go2_dashboard.go2_voice_playback import try_play_mp3_on_unitree_voice_service

    return try_play_mp3_on_unitree_voice_service(b64_mp3)


def hermes_try_play_tts_mp3_on_local_host(b64_mp3: str) -> bool:
    """Decode MP3 base64 and play on the **dashboard host** (Jetson) if env enabled.

    Requires ``GO2_HERMES_PLAY_ON_NX=1``, ``GO2_LOCAL=1``, and ``mpg123`` or ``ffplay`` on PATH,
    plus a working ALSA/Pulse output (e.g. USB speaker on the NX — audio still does not come from
    the Go2 internal buzzer unless routed there by your hardware setup).
    """
    from go2_dashboard.operator_stack import go2_local

    flag = (os.environ.get("GO2_HERMES_PLAY_ON_NX") or "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return False
    if not go2_local():
        return False
    if not b64_mp3 or not isinstance(b64_mp3, str) or not b64_mp3.strip():
        return False

    try:
        import base64
        import shutil
        import subprocess
        import tempfile

        data = base64.b64decode(b64_mp3.strip(), validate=False)
    except Exception:
        return False
    if not data:
        return False

    path = ""
    try:
        fd, path = tempfile.mkstemp(prefix="hermes_tts_", suffix=".mp3")
        os.write(fd, data)
        os.close(fd)
    except Exception:
        return False

    cmds: list[list[str]] = []
    if shutil.which("mpg123"):
        cmds.append(["mpg123", "-q", path])
    if shutil.which("ffplay"):
        cmds.append(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path])

    started = False
    for cmd in cmds:
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            started = True
            break
        except Exception:
            continue

    if not started:
        try:
            os.unlink(path)
        except Exception:
            pass
        return False

    return True


def _operator_memory_event_matches_hermes(ev: dict[str, Any]) -> bool:
    tags = ev.get("tags")
    if not isinstance(tags, list):
        return False
    for t in tags:
        if "hermes" in str(t).lower():
            return True
    return False


def hermes_operator_memory_block_for_prompt() -> str:
    """Blocchi di testo compatti dagli ultimi eventi `operator_session_memory` taggati hermes.

    Env:
    - ``GO2_HERMES_OPERATOR_MEMORY_LINES``: quante righe recenti *matching* includere (0 = disabilita).
      Default 14.
    """
    raw = (os.environ.get("GO2_HERMES_OPERATOR_MEMORY_LINES") or "14").strip()
    try:
        max_events = int(raw)
    except ValueError:
        max_events = 14
    if max_events <= 0:
        return ""

    scan = max(60, max_events * 8)
    events = read_recent_operator_session_events(scan)
    picked: list[dict[str, Any]] = []
    for ev in reversed(events):
        if not isinstance(ev, dict):
            continue
        if not _operator_memory_event_matches_hermes(ev):
            continue
        picked.append(ev)
        if len(picked) >= max_events:
            break
    picked.reverse()

    max_note = 260
    max_title = 80
    budget = 3600
    lines_out: list[str] = []
    used = 0
    for ev in picked:
        ts = str(ev.get("ts") or "").strip()
        day = ts[:10] if len(ts) >= 10 else ts or "?"
        title = str(ev.get("title") or "").strip()
        note = str(ev.get("note") or "").strip()
        if title:
            title = title[:max_title] + ("…" if len(str(ev.get("title") or "")) > max_title else "")
        if note:
            note = note[:max_note] + ("…" if len(str(ev.get("note") or "")) > max_note else "")
        parts = [day]
        if title:
            parts.append(title)
        head = " · ".join(parts)
        line = f"- {head}: {note}".strip()
        if len(line) < 8:
            continue
        if used + len(line) + 1 > budget:
            break
        lines_out.append(line)
        used += len(line) + 1

    if not lines_out:
        return ""
    body = "\n".join(lines_out)
    return (
        "--- Operator memory (dashboard JSONL, tag contains `hermes`) — concise facts / corrections ---\n"
        + body
        + "\n--- End operator memory ---"
    )


def _env_truthy_default_on(raw: str | None) -> bool:
    if raw is None or not str(raw).strip():
        return True
    s = str(raw).strip().lower()
    if s in {"0", "false", "no", "off"}:
        return False
    return s in {"1", "true", "yes", "on"}


def hermes_should_log_turn_to_memory(body: dict[str, Any]) -> bool:
    """POST ``log_turn_to_memory`` ha priorità; altrimenti ``GO2_HERMES_LOG_TURNS_TO_MEMORY`` (default on)."""
    if "log_turn_to_memory" in body:
        return bool(body.get("log_turn_to_memory"))
    return _env_truthy_default_on(os.environ.get("GO2_HERMES_LOG_TURNS_TO_MEMORY"))


def hermes_summarize_intent_it(intent: dict[str, Any]) -> str:
    """Riga leggibile per anteprima / approvazione operatore."""
    if not isinstance(intent, dict):
        return "Intent non valido."
    parts: list[str] = []
    bm = intent.get("base_motion")
    if isinstance(bm, dict) and bm.get("mode"):
        mode = str(bm.get("mode") or "").strip()
        en = bm.get("enable")
        parts.append(f"Base Sport: «{mode}»" + ("" if en is None else f" (enable={en})"))
    ap = intent.get("arm_preset")
    if isinstance(ap, str) and ap.strip():
        parts.append(f"Braccio preset: «{ap.strip()}»")
    aj = intent.get("arm_joint_delta")
    if isinstance(aj, dict) and aj.get("joint_index") is not None:
        try:
            ji = int(aj.get("joint_index"))
            dd = float(aj.get("delta_deg"))
            parts.append(f"Jog giunto {ji}: {dd}°")
        except (TypeError, ValueError):
            parts.append("Jog giunti (dettaglio non numerico)")
    att = intent.get("arm_tool_target")
    if isinstance(att, dict):
        raw = att.get("xyz_base_link_m")
        if isinstance(raw, (list, tuple)) and len(raw) >= 3:
            try:
                x, y, z = float(raw[0]), float(raw[1]), float(raw[2])
                bl = float(att.get("approach_blend") or 0)
                parts.append(
                    f"IK visione → base_link [{x:.3f}, {y:.3f}, {z:.3f}] m, blend {bl:.2f}"
                )
            except (TypeError, ValueError):
                parts.append("IK visione (coordinate non valide)")
    if not parts:
        return "Nessuna azione robot nel JSON (solo risposta testuale o campi null)."
    return " · ".join(parts)


def _hermes_compact_steps_for_memory(steps: Any) -> str:
    if not isinstance(steps, list) or not steps:
        return ""
    parts: list[str] = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        kind = str(s.get("kind") or "?")
        if kind == "base_motion":
            hs = s.get("http_status")
            res = s.get("result")
            ok = None
            if isinstance(res, dict):
                ok = res.get("ok")
            parts.append(f"base_motion http={hs} ok={ok}".strip())
        elif kind == "arm_preset":
            parts.append(f"arm_preset={s.get('preset')} ok={(s.get('result') or {}).get('ok')}")
        elif kind == "arm_joint_delta":
            parts.append(
                f"arm_delta j={s.get('joint_index')} ok={(s.get('result') or {}).get('ok')}"
            )
        elif kind == "arm_tool_target_partial":
            parts.append(f"arm_tool_target_partial ok={(s.get('result') or {}).get('ok')}")
        elif kind == "hermes_pre_move_hold":
            parts.append(f"pre_hold ok={(s.get('result') or {}).get('ok')}")
        elif kind == "openvla_plan":
            pl = s.get("result")
            pok = pl.get("ok") if isinstance(pl, dict) else None
            parts.append(f"openvla_plan http={s.get('http_status')} ok={pok}")
        elif kind == "openvla_execute":
            parts.append(f"openvla_exec ok={(s.get('result') or {}).get('ok')}")
        else:
            parts.append(kind)
    return "; ".join(parts)[:900]


def hermes_append_turn_log_memory(
    *,
    base_cmd: str,
    reply: str,
    intent_eff: dict[str, Any],
    applied: dict[str, Any],
    dry_run: bool,
    body: dict[str, Any],
) -> None:
    """Append-only JSONL: ripesca nei turni successivi se «Includi memoria» è attivo (tag hermes)."""
    if dry_run or not hermes_should_log_turn_to_memory(body):
        return
    cmd = (base_cmd or "").strip()
    if not cmd:
        return
    steps = applied.get("steps")
    step_txt = _hermes_compact_steps_for_memory(steps)
    bm = intent_eff.get("base_motion")
    bm_mode = ""
    if isinstance(bm, dict) and bm.get("mode"):
        bm_mode = str(bm.get("mode"))
    note_parts = [
        f"Assistant: {(reply or '').strip()[:1200]}",
        f"Steps: {step_txt}" if step_txt else "Steps: (none)",
    ]
    if bm_mode:
        note_parts.append(f"Intent base_motion.mode={bm_mode}")
    note = "\n".join(note_parts)
    if len(note) > 7800:
        note = note[:7790] + "…"
    title = ("Turn: " + cmd)[:200]
    try:
        append_operator_session_event(
            {
                "title": title,
                "note": note,
                "tags": ["hermes", "turn_log"],
                "data": {"ok_chain": bool(applied.get("ok"))},
            }
        )
    except Exception:
        pass


"""Calibrazione presa manuale: detection Orbbec → rilascio giunti → operatore posa braccio → memoria.

Flusso (default 4 s hold + 15 s teach):
  1. Acquisizione metrica (oggetto rilevato + piano IK grasp).
  2. Attesa con coppia attiva (braccio fermo).
  3. ``motor_release`` (funcode 5 mode 0) — giunti liberi per posizionamento manuale.
  4. Lettura ``servo_deg`` insegnata, delta rispetto al grasp IK pianificato.
  5. Salvataggio in ``data/grasp_teach_calibration.json`` e ri-accoppiamento sulla posa insegnata.

Le registrazioni forniscono un **offset di calibrazione** (Δ giunti / Δ TCP rispetto all'IK
automatico) riapplicato in ``plan_wrist_grasp_metric`` sul target **dinamico** corrente —
non sostituiscono la presa con la posa fissa insegnata.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT

_CALIB_PATH = PROJECT_ROOT / "data" / "grasp_teach_calibration.json"
_MOUNT_BASE_LINK_M = (0.15, 0.0, 0.06)

_STATE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "active": False,
    "phase": "idle",
    "phase_label_it": "",
    "started_at": None,
    "ends_at": None,
    "remaining_s": 0.0,
    "error": None,
    "last_sample": None,
    "cancel_requested": False,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_float(key: str, default: float) -> float:
    try:
        return float((os.environ.get(key) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def calib_path() -> str:
    return str(_CALIB_PATH)


def _tcp_base_link_from_servo_deg(servo_deg: list[float]) -> list[float]:
    import sys

    s = str(PROJECT_ROOT / "scripts")
    if s not in sys.path:
        sys.path.insert(0, s)
    import arm_kinematics_d1_template as K

    q = [math.radians(float(servo_deg[i])) for i in range(6)]
    tip = K.fk_tool_tip(q)
    return [round(float(tip[i] + _MOUNT_BASE_LINK_M[i]), 4) for i in range(3)]


def _norm7(sd: list[float]) -> list[float]:
    out = [round(float(sd[i]), 3) for i in range(min(7, len(sd)))]
    while len(out) < 7:
        out.append(out[-1] if out else 0.0)
    return out


def _grasp_servo_from_plan(preview_plan: list[dict[str, Any]] | None) -> list[float] | None:
    if not preview_plan:
        return None
    for st in preview_plan:
        if st.get("stage") == "grasp" and st.get("servo_deg"):
            return _norm7([float(x) for x in st["servo_deg"]])
    for st in reversed(preview_plan):
        if st.get("servo_deg"):
            return _norm7([float(x) for x in st["servo_deg"]])
    return None


def load_calibration_store() -> dict[str, Any]:
    try:
        data = json.loads(_CALIB_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("version", 1)
            data.setdefault("samples", [])
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "samples": []}


def save_calibration_store(store: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("GO2_LOCAL", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "go2_local_off"}
    _CALIB_PATH.parent.mkdir(parents=True, exist_ok=True)
    store["updated_at"] = _now_iso()
    _CALIB_PATH.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "path": str(_CALIB_PATH), "samples": len(store.get("samples") or [])}


def teach_calib_status() -> dict[str, Any]:
    with _STATE_LOCK:
        st = dict(_STATE)
    store = load_calibration_store()
    return {
        "ok": True,
        "active": bool(st.get("active")),
        "phase": st.get("phase") or "idle",
        "phase_label_it": st.get("phase_label_it") or "",
        "remaining_s": round(float(st.get("remaining_s") or 0.0), 1),
        "error": st.get("error"),
        "last_sample": st.get("last_sample"),
        "samples_count": len(store.get("samples") or []),
        "calib_path": str(_CALIB_PATH),
        "hold_s_default": _env_float("GO2_GRASP_TEACH_HOLD_S", 4.0),
        "manual_s_default": _env_float("GO2_GRASP_TEACH_MANUAL_S", 15.0),
    }


def _set_state(**kwargs: Any) -> None:
    with _STATE_LOCK:
        _STATE.update(kwargs)


def _sleep_abortable(total_s: float, phase: str, label_it: str) -> bool:
    """Sleep a frazioni aggiornando remaining_s; False se cancel."""
    end = time.time() + max(0.0, total_s)
    while time.time() < end:
        with _STATE_LOCK:
            if _STATE.get("cancel_requested"):
                return False
        rem = max(0.0, end - time.time())
        _set_state(phase=phase, phase_label_it=label_it, remaining_s=rem)
        time.sleep(min(0.25, rem))
    return True


def _metric_plan_for_teach(instruction: str = "") -> dict[str, Any]:
    from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag
    from go2_dashboard.orbbec_wrist_grasp import plan_wrist_grasp_metric

    servo_now, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if servo_now is None or len(servo_now) < 6:
        return {"ok": False, "reason": "no_servo_feedback", "diag": diag}
    return plan_wrist_grasp_metric([float(x) for x in servo_now[:7]], instruction=instruction or None)


def _build_sample_from_metric(
    mp: dict[str, Any],
    *,
    taught_servo_deg: list[float],
    at_start_servo_deg: list[float],
) -> dict[str, Any]:
    det = mp.get("object_detection") if isinstance(mp.get("object_detection"), dict) else {}
    preview = mp.get("preview") if isinstance(mp.get("preview"), dict) else {}
    plan = preview.get("plan") if isinstance(preview.get("plan"), list) else []
    planned = _grasp_servo_from_plan(plan)
    taught = _norm7(taught_servo_deg)
    at_start = _norm7(at_start_servo_deg)
    delta = [round(taught[i] - (planned[i] if planned else at_start[i]), 3) for i in range(7)]
    tgt = mp.get("grasp_display_base_link_m") or (mp.get("target") or {}).get("base_xyz_m")
    planned_tcp = _tcp_base_link_from_servo_deg(planned or at_start)
    taught_tcp = _tcp_base_link_from_servo_deg(taught)
    delta_tcp = [round(taught_tcp[i] - planned_tcp[i], 4) for i in range(3)]
    return {
        "id": str(uuid.uuid4())[:8],
        "saved_at": _now_iso(),
        "detection": {
            "norm": det.get("norm"),
            "bbox_center_px": det.get("bbox_center_px"),
            "bbox_xyxy": det.get("bbox_xyxy"),
            "confidence": det.get("confidence"),
            "backend": det.get("backend"),
            "orientation_deg": det.get("orientation_deg"),
        },
        "metric": {
            "target_base_link_m": tgt,
            "depth_m": mp.get("depth_m"),
            "camera_xyz_m": mp.get("camera_xyz_m"),
            "reach_m": mp.get("reach_m"),
        },
        "at_start": {"servo_deg": at_start},
        "planned": {
            "grasp_servo_deg": planned,
            "grasp_tcp_base_link_m": planned_tcp,
        },
        "taught": {
            "grasp_servo_deg": taught,
            "grasp_tcp_base_link_m": taught_tcp,
        },
        "delta": {
            "servo_deg": delta,
            "tcp_base_link_m": delta_tcp,
        },
    }


def _teach_worker(
    *,
    hold_s: float,
    manual_s: float,
    instruction: str,
    pending: dict[str, Any],
) -> None:
    try:
        if not _sleep_abortable(hold_s, "hold", f"Coppia attiva — preparati ({hold_s:.0f}s)…"):
            _set_state(active=False, phase="cancelled", phase_label_it="Annullato", remaining_s=0.0)
            return

        from go2_dashboard.d1_jog import service as jog_svc

        _set_state(phase="releasing", phase_label_it="Rilascio giunti (coppia OFF)…", remaining_s=0.0)
        rel = jog_svc.motor_release()
        if not rel.get("ok"):
            _set_state(
                active=False,
                phase="error",
                error=rel.get("reason") or "motor_release_failed",
                phase_label_it=(rel.get("hint_it") or "Errore rilascio giunti"),
            )
            return

        if not _sleep_abortable(
            manual_s,
            "teach_manual",
            f"Giunti liberi — porta il braccio in posa di presa ({manual_s:.0f}s)…",
        ):
            _set_state(active=False, phase="cancelled", phase_label_it="Annullato", remaining_s=0.0)
            return

        _set_state(phase="capture", phase_label_it="Memorizzo posa insegnata…", remaining_s=0.0)
        from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag

        taught, diag = read_servo_deg_with_diag(PROJECT_ROOT)
        if taught is None or len(taught) < 6:
            _set_state(
                active=False,
                phase="error",
                error="no_servo_feedback_after_teach",
                phase_label_it="Feedback giunti assente dopo teach",
            )
            return

        mp = pending.get("metric_plan") or {}
        sample = _build_sample_from_metric(
            mp,
            taught_servo_deg=[float(x) for x in taught[:7]],
            at_start_servo_deg=pending.get("at_start_servo_deg") or [],
        )
        store = load_calibration_store()
        samples = list(store.get("samples") or [])
        samples.append(sample)
        store["samples"] = samples[-int(_env_float("GO2_GRASP_TEACH_MAX_SAMPLES", 24)) :]
        save_calibration_store(store)

        _set_state(phase="recover", phase_label_it="Ri-accoppio sulla posa insegnata…", remaining_s=0.0)
        jog_svc.ensure_coupled(with_power=False)
        jog_svc.joint_control_begin(servo_deg=sample["taught"]["grasp_servo_deg"])

        _set_state(
            active=False,
            phase="done",
            phase_label_it="Calibrazione salvata — coppia ripristinata sulla posa insegnata.",
            remaining_s=0.0,
            last_sample=sample,
            error=None,
        )
    except Exception as exc:
        _set_state(
            active=False,
            phase="error",
            error=repr(exc),
            phase_label_it="Errore calibrazione teach",
            remaining_s=0.0,
        )


def teach_calib_start(
    *,
    instruction: str = "",
    hold_s: float | None = None,
    manual_s: float | None = None,
    require_detection: bool = True,
) -> dict[str, Any]:
    """Avvia sessione teach (thread in background). Richiede oggetto rilevato."""
    with _STATE_LOCK:
        if _STATE.get("active"):
            return {"ok": False, "reason": "teach_session_active", "status": teach_calib_status()}

    if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "GO2_ENABLE_REAL_ARM_off"}

    hold = hold_s if hold_s is not None else _env_float("GO2_GRASP_TEACH_HOLD_S", 4.0)
    manual = manual_s if manual_s is not None else _env_float("GO2_GRASP_TEACH_MANUAL_S", 15.0)
    hold = max(1.0, min(hold, 30.0))
    manual = max(5.0, min(manual, 120.0))

    mp = _metric_plan_for_teach(instruction)
    det = mp.get("object_detection") if isinstance(mp.get("object_detection"), dict) else {}
    if require_detection and (not mp.get("ok") or not det.get("ok")):
        return {
            "ok": False,
            "reason": mp.get("reason") or "no_detection",
            "hint_it": "Esegui prima «Acquisizione e stima» con oggetto visibile, poi calibra.",
            "metric_plan": {"ok": mp.get("ok"), "reason": mp.get("reason")},
        }

    from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag

    at_start, _ = read_servo_deg_with_diag(PROJECT_ROOT)
    if at_start is None:
        return {"ok": False, "reason": "no_servo_feedback"}

    pending = {
        "metric_plan": mp,
        "at_start_servo_deg": _norm7([float(x) for x in at_start[:7]]),
        "hold_s": hold,
        "manual_s": manual,
    }
    _set_state(
        active=True,
        phase="hold",
        phase_label_it=f"Oggetto rilevato — coppia attiva ({hold:.0f}s prima del rilascio)…",
        started_at=time.time(),
        remaining_s=hold,
        error=None,
        last_sample=None,
        cancel_requested=False,
    )
    th = threading.Thread(
        target=_teach_worker,
        kwargs={"hold_s": hold, "manual_s": manual, "instruction": instruction, "pending": pending},
        name="grasp_teach_calib",
        daemon=True,
    )
    th.start()
    return {
        "ok": True,
        "started": True,
        "hold_s": hold,
        "manual_s": manual,
        "detection": {
            "norm": det.get("norm"),
            "bbox_center_px": det.get("bbox_center_px"),
            "confidence": det.get("confidence"),
        },
        "target_base_link_m": mp.get("grasp_display_base_link_m"),
        "status": teach_calib_status(),
        "hint_it": (
            f"Dopo {hold:.0f}s i giunti verranno rilasciati per {manual:.0f}s: "
            "porta manualmente il braccio in posa di presa corretta."
        ),
    }


def teach_calib_cancel() -> dict[str, Any]:
    with _STATE_LOCK:
        if not _STATE.get("active"):
            return {"ok": True, "cancelled": False, "reason": "not_active"}
        _STATE["cancel_requested"] = True
    return {"ok": True, "cancelled": True, "status": teach_calib_status()}


def teach_calib_list_samples() -> dict[str, Any]:
    store = load_calibration_store()
    samples = store.get("samples") or []
    brief = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        brief.append(
            {
                "id": s.get("id"),
                "saved_at": s.get("saved_at"),
                "norm": (s.get("detection") or {}).get("norm"),
                "target_base_link_m": (s.get("metric") or {}).get("target_base_link_m"),
                "delta_servo_deg": (s.get("delta") or {}).get("servo_deg"),
            }
        )
    return {"ok": True, "samples": brief, "count": len(brief), "path": str(_CALIB_PATH)}


def teach_calib_clear() -> dict[str, Any]:
    out = save_calibration_store({"version": 1, "samples": []})
    return {**out, "cleared": True}


def _match_score(det: dict[str, Any], sample: dict[str, Any], target_bl: list[float] | None) -> float:
    """Punteggio più basso = match migliore."""
    sdet = sample.get("detection") if isinstance(sample.get("detection"), dict) else {}
    sn = sdet.get("norm")
    dn = det.get("norm")
    score = 10.0
    if isinstance(sn, (list, tuple)) and isinstance(dn, (list, tuple)) and len(sn) >= 2 and len(dn) >= 2:
        du = float(dn[0]) - float(sn[0])
        dv = float(dn[1]) - float(sn[1])
        score = min(score, math.hypot(du, dv))
    sm = sample.get("metric") if isinstance(sample.get("metric"), dict) else {}
    st = sm.get("target_base_link_m")
    if isinstance(st, (list, tuple)) and isinstance(target_bl, (list, tuple)) and len(st) >= 3 and len(target_bl) >= 3:
        d3 = math.sqrt(sum((float(target_bl[i]) - float(st[i])) ** 2 for i in range(3)))
        score = min(score, d3 * 2.0)
    return score


def find_best_teach_sample(
    det: dict[str, Any],
    target_base_link_m: list[float] | None,
) -> tuple[dict[str, Any] | None, float]:
    store = load_calibration_store()
    samples = [s for s in (store.get("samples") or []) if isinstance(s, dict)]
    if not samples:
        return None, 999.0
    norm_max = _env_float("GO2_GRASP_TEACH_MATCH_NORM_MAX", 0.22)
    scored = sorted(((s, _match_score(det, s, target_base_link_m)) for s in samples), key=lambda t: t[1])
    best, sc = scored[0]
    if sc > norm_max:
        return None, sc
    return best, sc


def _ik_module():
    import sys

    s = str(PROJECT_ROOT / "scripts")
    if s not in sys.path:
        sys.path.insert(0, s)
    import arm_kinematics_d1_template as K

    return K


def _reach_m_from_base_link(target_bl: list[float]) -> float:
    tip_fk = [float(target_bl[i]) - float(_MOUNT_BASE_LINK_M[i]) for i in range(3)]
    origin = list(_MOUNT_BASE_LINK_M)
    return math.sqrt(sum((tip_fk[i] - origin[i]) ** 2 for i in range(3)))


def _apply_tcp_offset_to_stage(
    st: dict[str, Any],
    delta_tcp: list[float],
    *,
    seed_rad: list[float] | None,
) -> tuple[bool, list[float] | None]:
    tgt = st.get("target_xyz_m")
    if not isinstance(tgt, list) or len(tgt) < 3:
        return False, seed_rad
    new_tgt = [round(float(tgt[i]) + float(delta_tcp[i]), 4) for i in range(3)]
    st["target_xyz_m"] = new_tgt
    st["teach_calib_offset"] = True

    K = _ik_module()
    tip_arm = [new_tgt[i] - float(_MOUNT_BASE_LINK_M[i]) for i in range(3)]
    q_seed = list(seed_rad[:6]) if isinstance(seed_rad, list) and len(seed_rad) >= 6 else [0.0] * 6
    sol = K.ik_reach(tip_arm[0], tip_arm[1], tip_arm[2], primary_seed=q_seed)
    if sol is None:
        st["ik_ok"] = False
        return False, seed_rad
    tip = K.fk_tool_tip(sol)
    joints = [round(float(x), 4) for x in sol]
    st["joints_rad"] = joints
    st["servo_deg"] = [round(math.degrees(float(x)), 2) for x in sol]
    st["fk_tip_xyz_m"] = [round(float(tip[i] + _MOUNT_BASE_LINK_M[i]), 4) for i in range(3)]
    st["ik_ok"] = True
    return True, joints


def _apply_joint_offset_to_stage(st: dict[str, Any], delta_servo: list[float]) -> bool:
    planned = st.get("servo_deg")
    if not isinstance(planned, list) or len(planned) < 6:
        return False
    corrected = [round(float(planned[i]) + float(delta_servo[i]), 2) for i in range(6)]
    st["servo_deg"] = corrected
    st["joints_rad"] = [round(math.radians(float(corrected[i])), 4) for i in range(6)]
    st["teach_calib_offset"] = True
    st["fk_tip_xyz_m"] = _tcp_base_link_from_servo_deg(corrected)
    if isinstance(st.get("target_xyz_m"), list) and len(st["target_xyz_m"]) >= 3:
        st["target_xyz_m"] = list(st["fk_tip_xyz_m"])
    st["ik_ok"] = True
    return True


def apply_teach_to_metric_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Applica offset calibrazione teach al piano metrico dinamico (in-place).

    Il target 3D resta quello della visione corrente; si aggiunge solo il Δ misurato
    con i giunti smollati (errore sistematico IK / camera / pinza).
    """
    if not plan.get("ok"):
        return plan
    det = plan.get("object_detection") if isinstance(plan.get("object_detection"), dict) else {}
    if not det.get("ok"):
        return plan
    tgt = plan.get("grasp_display_base_link_m")
    sample, score = find_best_teach_sample(det, tgt if isinstance(tgt, list) else None)
    if not sample:
        plan["teach_calib_applied"] = False
        return plan

    delta_block = sample.get("delta") if isinstance(sample.get("delta"), dict) else {}
    delta_tcp = delta_block.get("tcp_base_link_m")
    delta_servo = delta_block.get("servo_deg")
    if not (isinstance(delta_tcp, list) and len(delta_tcp) >= 3) and not (
        isinstance(delta_servo, list) and len(delta_servo) >= 6
    ):
        plan["teach_calib_applied"] = False
        return plan

    preview = plan.get("preview") if isinstance(plan.get("preview"), dict) else {}
    stages = preview.get("plan") if isinstance(preview.get("plan"), list) else []
    seed: list[float] | None = None
    for st in stages:
        if isinstance(st.get("joints_rad"), list) and len(st["joints_rad"]) >= 6:
            seed = st["joints_rad"]
            break

    applied = 0
    if isinstance(delta_tcp, list) and len(delta_tcp) >= 3:
        for st in stages:
            ok, seed = _apply_tcp_offset_to_stage(st, delta_tcp, seed_rad=seed)
            if ok:
                applied += 1
    elif isinstance(delta_servo, list) and len(delta_servo) >= 6:
        for st in stages:
            if st.get("stage") == "grasp" and _apply_joint_offset_to_stage(st, delta_servo):
                applied += 1
                break

    if applied <= 0:
        plan["teach_calib_applied"] = False
        return plan

    ik_all_ok = all(bool(st.get("ik_ok")) for st in stages if st.get("target_xyz_m"))
    preview["ok"] = ik_all_ok

    if isinstance(delta_servo, list):
        plan["teach_calib_delta_servo_deg"] = [round(float(x), 2) for x in delta_servo[:6]]
    if isinstance(delta_tcp, list):
        plan["teach_calib_delta_tcp_m"] = [round(float(x), 4) for x in delta_tcp[:3]]

    vision_grasp = plan.get("grasp_display_base_link_m")
    if isinstance(vision_grasp, list) and len(vision_grasp) >= 3 and isinstance(delta_tcp, list):
        corrected_grasp = [round(float(vision_grasp[i]) + float(delta_tcp[i]), 4) for i in range(3)]
        plan["grasp_display_base_link_m"] = corrected_grasp
        if isinstance(plan.get("target"), dict):
            plan["target"]["base_xyz_m"] = list(corrected_grasp)
            plan["target"]["source"] = "orbbec_metric_wrist+teach_offset"
        reach_m = _reach_m_from_base_link(corrected_grasp)
        max_reach = _env_float("GO2_ARM_MAX_REACH_M", 0.55)
        reachable = reach_m <= max_reach
        plan["reach_m"] = round(reach_m, 4)
        plan["reachable"] = reachable
        execution_allowed = bool(reachable and ik_all_ok)
        plan["absolute_ik_safe"] = execution_allowed
        if isinstance(plan.get("grasp_assessment"), dict):
            plan["grasp_assessment"]["execution_allowed"] = execution_allowed
            plan["grasp_assessment"]["reachable"] = reachable
            plan["grasp_assessment"]["reach_m"] = round(reach_m, 4)
            plan["grasp_assessment"]["label_it"] = (
                "3D Orbbec dinamico + offset calibrazione teach"
                if execution_allowed
                else "Offset teach applicato — reach/IK da verificare"
            )
        if isinstance(plan.get("validation_ui"), dict):
            plan["validation_ui"]["can_execute_ik"] = execution_allowed
            plan["validation_ui"]["can_execute_phased"] = execution_allowed
            plan["validation_ui"]["ok"] = execution_allowed

    plan["teach_calib_applied"] = True
    plan["teach_calib_sample_id"] = sample.get("id")
    plan["teach_calib_match_score"] = round(score, 4)
    plan["teach_calib_mode"] = "offset"
    return plan

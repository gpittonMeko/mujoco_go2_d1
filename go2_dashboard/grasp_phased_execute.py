"""Esecuzione a fasi del preview IK (pre_grasp → approach → grasp → lift) sulla NX."""

from __future__ import annotations

import os
import time
from typing import Any

from go2_dashboard.grasp_assessment import worker_flat_plan_assessment
from go2_dashboard.operator_plan_cache import get_last_grasp_plan


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or str(default)).strip())
    except ValueError:
        return default


def _env_truthy(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _stage_delay_ms() -> int:
    return max(80, _env_int("GO2_GRASP_PHASE_DELAY_MS", 400))


def _env_float(name: str, fallback: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        return fallback


def _gripper_plan_by_stage(plan: dict[str, Any]) -> dict[str, str]:
    preview = plan.get("preview") or {}
    rows = preview.get("gripper") or []
    out: dict[str, str] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        st = str(row.get("stage") or "").strip()
        cmd = str(row.get("gripper") or "").strip().lower()
        if st and cmd:
            out[st] = cmd
    return out


def _grasp_hold_s(plan: dict[str, Any]) -> float:
    preview = plan.get("preview") or {}
    rows = preview.get("gripper") or []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and str(row.get("stage") or "") == "grasp":
                try:
                    return float(row.get("hold_s") or 0.6)
                except (TypeError, ValueError):
                    return 0.6
    return 0.6


def _verify_grasp_stage(plan: dict[str, Any]) -> dict[str, Any]:
    """Verifica post-presa (stallo posizione pinza) dopo lo stadio ``grasp``."""
    from go2_dashboard.grasp_close_verify import verify_gripper_grasp

    close_deg = _gripper_deg_for_command("close")
    return verify_gripper_grasp(close_deg if close_deg is not None else -14.0, hold_s=_grasp_hold_s(plan))


def _gripper_deg_for_command(cmd: str) -> float | None:
    c = (cmd or "").strip().lower()
    if c == "open":
        return _env_float("GO2_GRASP_GRIPPER_OPEN_DEG", _env_float("GO2_GRASP_COACH_GRIPPER_OPEN_DEG", 22.0))
    if c in {"close", "hold_closed", "hold"}:
        return _env_float("GO2_GRASP_GRIPPER_CLOSE_DEG", _env_float("GO2_GRASP_COACH_GRIPPER_CLOSE_DEG", -14.0))
    return None


def _apply_gripper_for_stage(stage_name: str, gripper_by_stage: dict[str, str]) -> dict[str, Any] | None:
    if not _env_truthy("GO2_GRASP_PHASE_EXECUTE_GRIPPER", "1"):
        return None
    cmd = gripper_by_stage.get(stage_name)
    if not cmd:
        return None
    deg = _gripper_deg_for_command(cmd)
    if deg is None:
        return None
    from go2_dashboard.d1_arm_publish_lite import publish_move_one_joint_deg

    return publish_move_one_joint_deg(6, deg)


def _prepare_grasp_motion_session() -> dict[str, Any]:
    """Tab Giunti: couple + ``session_begin`` (live DDS), come ``operators_arm_joints.js``."""
    from go2_dashboard import d1_arm_motion

    return d1_arm_motion.ensure_grasp_motion_worker()


def _hold_between_phases() -> dict[str, Any]:
    """Tra una fase e l'altra: hold sul daemon (stesso stream del tab Giunti)."""
    if not _env_truthy("GO2_GRASP_PHASE_HOLD_BETWEEN", "1"):
        return {"ok": True, "skipped": True, "reason": "GO2_GRASP_PHASE_HOLD_BETWEEN_off"}
    from go2_dashboard.d1_arm_publish_lite import _use_operator_arm_motion

    if _use_operator_arm_motion():
        from go2_dashboard.operator_arm_motion import hold_operator_arm_pose

        rpt = max(1, min(4, _env_int("GO2_GRASP_PHASE_HOLD_REPEATS", 2)))
        holds = [hold_operator_arm_pose() for _ in range(rpt)]
        ok = all(h.get("ok") or h.get("skipped") for h in holds)
        return {"ok": ok, "stream": True, "holds": holds, "action": "phase_hold", "motion_path": "operator_ui_hold"}

    from go2_dashboard.d1_jog import service as jog_svc

    fb = jog_svc.read_servo_deg(fast=True)
    if not fb.get("ok") or not fb.get("servo_deg"):
        return {"ok": False, "reason": "no_feedback", "action": "phase_hold"}
    rpt = max(2, min(6, _env_int("GO2_GRASP_PHASE_HOLD_REPEATS", 4)))
    holds = [jog_svc.hold_pose_stream(servo_deg=fb["servo_deg"]) for _ in range(rpt)]
    return {"ok": True, "stream": True, "holds": holds, "action": "phase_hold"}


def _settle_after_stage() -> dict[str, Any]:
    hold = _hold_between_phases()
    delay = _stage_delay_ms()
    if delay > 0:
        time.sleep(delay / 1000.0)
    return hold


def _stage_motion_summary(st: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "stage": st.get("stage"),
        "motion_ok": r.get("ok"),
        "motion_mode": r.get("mode"),
        "path_points": r.get("path_points"),
        "traj_delay_ms": r.get("traj_delay_ms"),
        "reason": r.get("reason"),
    }
    t = st.get("target_xyz_m")
    if isinstance(t, (list, tuple)) and len(t) >= 3:
        row["target_xyz_m"] = [round(float(t[i]), 4) for i in range(3)]
    jr = st.get("joints_rad")
    if isinstance(jr, (list, tuple)) and len(jr) >= 6:
        row["joints_rad_6"] = [round(float(jr[i]), 4) for i in range(6)]
    if r.get("ik_target_base_link_m"):
        row["ik_target_base_link_m"] = r.get("ik_target_base_link_m")
    return row


def execute_phased_from_cached_plan(
    *,
    confirm: str | None = None,
    max_stages: int | None = None,
    allow_heuristic_override: bool | None = None,
) -> dict[str, Any]:
    if confirm != "EXECUTE_PHASED_GRASP":
        return {
            "ok": False,
            "reason": "confirm_required",
            "hint_it": 'Invia JSON {"confirm":"EXECUTE_PHASED_GRASP"}.',
        }
    plan = get_last_grasp_plan()
    if not isinstance(plan, dict) or not plan.get("ok"):
        return {"ok": False, "reason": "no_cached_plan", "hint_it": "Prima POST /api/grasp/plan ok."}

    assessment = worker_flat_plan_assessment(plan)
    allow_heur = allow_heuristic_override
    if allow_heur is None:
        allow_heur = os.environ.get("GO2_GRASP_ALLOW_HEURISTIC_EXECUTE", "0").lower() in {"1", "true", "yes", "on"}
    if not assessment.get("execution_allowed") and not (allow_heur and assessment.get("preview_only")):
        return {
            "ok": False,
            "reason": "execution_not_allowed",
            "grasp_assessment": assessment,
            "hint_it": assessment.get("hint_it")
            or "Piano non validato 3D — serve depth/tags o GO2_GRASP_ALLOW_HEURISTIC_EXECUTE=1.",
        }

    preview = plan.get("preview") or {}
    stages = preview.get("plan") or []
    if not preview.get("ok") or not stages:
        xyz = plan.get("grasp_display_base_link_m")
        if isinstance(xyz, (list, tuple)) and len(xyz) >= 3:
            from go2_dashboard.d1_arm_publish_lite import goto_tool_target_base_link_m_staged

            session = _prepare_grasp_motion_session()
            out = goto_tool_target_base_link_m_staged(
                [float(xyz[0]), float(xyz[1]), float(xyz[2])], motion_profile="grasp"
            )
            out["mode"] = "single_ik_fallback"
            out["grasp_assessment"] = assessment
            out["motion_session"] = session
            _hold_between_phases()
            return out
        return {
            "ok": False,
            "reason": "no_preview_stages",
            "hint_it": "Il piano non contiene preview.plan — rigenera con target/depth ok.",
        }

    cap = max_stages if max_stages is not None else _env_int("GO2_GRASP_MAX_PHASES", 4)
    gripper_by_stage = _gripper_plan_by_stage(plan)
    from go2_dashboard.d1_arm_publish_lite import (
        goto_joints_rad_clamped_six_staged,
        goto_tool_target_base_link_m_staged,
    )

    motion_session = _prepare_grasp_motion_session()
    use_staged = os.environ.get("GO2_GRASP_STAGED_MOTION", "1").lower() in {"1", "true", "yes", "on"}
    results: list[dict[str, Any]] = []
    gripper_results: list[dict[str, Any]] = []
    step_log: list[dict[str, Any]] = []
    ok_all = True
    for i, st in enumerate(stages[:cap]):
        if not isinstance(st, dict):
            continue
        name = str(st.get("stage") or f"stage_{i}")
        jr = st.get("joints_rad")
        if isinstance(jr, (list, tuple)) and len(jr) >= 6:
            try:
                q = [float(jr[j]) for j in range(6)]
            except (TypeError, ValueError):
                q = None
            if q is not None:
                if use_staged:
                    r = goto_joints_rad_clamped_six_staged(q, motion_profile="grasp")
                else:
                    from go2_dashboard.d1_arm_publish_lite import goto_joints_rad_clamped_six

                    r = goto_joints_rad_clamped_six(q, motion_profile="grasp")
                r["stage"] = name
                results.append(r)
                step_log.append(_stage_motion_summary(st, r))
                if not r.get("ok"):
                    ok_all = False
                    break
                gr = _apply_gripper_for_stage(name, gripper_by_stage)
                if gr is not None:
                    gr["stage"] = name
                    gripper_results.append(gr)
                    step_log[-1]["gripper_ok"] = gr.get("ok")
                    if not gr.get("ok"):
                        ok_all = False
                        break
                    if name == "grasp" and gr.get("ok"):
                        verify = _verify_grasp_stage(plan)
                        step_log[-1]["grasp_verify"] = verify
                        if not verify.get("grasp_detected", True) and _env_truthy(
                            "GO2_GRASP_VERIFY_ABORT_LIFT", "0"
                        ):
                            ok_all = False
                            break
                settle = _settle_after_stage()
                step_log[-1]["inter_phase_hold_ok"] = settle.get("ok")
                continue
        t = st.get("target_xyz_m")
        if isinstance(t, (list, tuple)) and len(t) >= 3:
            tgt3 = [float(t[0]), float(t[1]), float(t[2])]
            if use_staged:
                r = goto_tool_target_base_link_m_staged(tgt3, motion_profile="grasp")
            else:
                from go2_dashboard.d1_arm_publish_lite import goto_tool_target_base_link_m

                r = goto_tool_target_base_link_m(tgt3, motion_profile="grasp")
            r["stage"] = name
            results.append(r)
            step_log.append(_stage_motion_summary(st, r))
            if not r.get("ok"):
                ok_all = False
                break
            gr = _apply_gripper_for_stage(name, gripper_by_stage)
            if gr is not None:
                gr["stage"] = name
                gripper_results.append(gr)
                step_log[-1]["gripper_ok"] = gr.get("ok")
                if not gr.get("ok"):
                    ok_all = False
                    break
                if name == "grasp" and gr.get("ok"):
                    verify = _verify_grasp_stage(plan)
                    step_log[-1]["grasp_verify"] = verify
                    if not verify.get("grasp_detected", True) and _env_truthy(
                        "GO2_GRASP_VERIFY_ABORT_LIFT", "0"
                    ):
                        ok_all = False
                        break
            settle = _settle_after_stage()
            step_log[-1]["inter_phase_hold_ok"] = settle.get("ok")

    if ok_all:
        final_hold = _hold_between_phases()
    else:
        final_hold = {"ok": False, "skipped": True, "reason": "aborted"}

    return {
        "ok": ok_all,
        "mode": "phased_preview",
        "motion_profile": "grasp",
        "motion_session": motion_session,
        "stages_run": len(results),
        "stage_results": results,
        "gripper_results": gripper_results,
        "step_log": step_log,
        "grasp_assessment": assessment,
        "failed_stage": None if ok_all else (results[-1].get("stage") if results else None),
        "final_hold": final_hold,
        "hint_it": (
            "Fasi con profilo grasp (D1_MAX_STEP_DEG_GRASP + hold tra fasi). "
            "Prima di provare: Coppia ON su d1_jog :5053 o D1_AUTO_COUPLE_ON_MOVE=1."
        ),
    }

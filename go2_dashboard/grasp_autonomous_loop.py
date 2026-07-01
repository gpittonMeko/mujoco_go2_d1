"""Loop presa autonoma closed-loop: Orbbec polso → visual servo → IK parziale → verify pinza."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT

CONFIRM_TOKEN = "RUN_AUTONOMOUS_GRASP"

_JOB_LOCK = threading.Lock()
_JOB: dict[str, Any] = {
    "running": False,
    "flow": "autonomous_grasp",
    "ok": None,
    "started_at": None,
    "finished_at": None,
    "current_step": None,
    "label_it": "Nessuna presa autonoma avviata.",
    "cycles": [],
    "params": None,
    "grasp_verify": None,
}


def _truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def autonomous_grasp_status() -> dict[str, Any]:
    with _JOB_LOCK:
        return json.loads(json.dumps({k: v for k, v in _JOB.items()}, default=str))


def _job_set(**kw: Any) -> None:
    with _JOB_LOCK:
        _JOB.update(kw)


def _job_cycle(entry: dict[str, Any]) -> None:
    with _JOB_LOCK:
        _JOB["cycles"].append(entry)
        _JOB["current_step"] = entry.get("step")


def _servo_deg7() -> list[float] | None:
    try:
        from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag

        sd, _ = read_servo_deg_with_diag(PROJECT_ROOT)
        if sd is None or len(sd) < 6:
            return None
        out = [float(sd[i]) for i in range(min(7, len(sd)))]
        while len(out) < 7:
            out.append(0.0)
        return out
    except Exception:
        return None


def _dbg_agent_log(location: str, message: str, data: dict[str, Any], hypothesis_id: str) -> None:
    # #region agent log
    try:
        import json

        payload = {
            "sessionId": "16a61f",
            "runId": "grasp-fix-v1",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        log_path = PROJECT_ROOT / "data" / "debug-16a61f.ndjson"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass
    # #endregion


def _attach_metric_depth_fields(entry: dict[str, Any], mp: dict[str, Any], det: dict[str, Any] | None = None) -> None:
    """Propaga depth/metrica al teach_status (polling UI RealSense)."""
    dd = mp.get("depth_diag") if isinstance(mp.get("depth_diag"), dict) else {}
    src = mp.get("depth_source")
    entry["depth_m"] = mp.get("depth_m")
    entry["depth_m_raw"] = mp.get("depth_m_raw")
    entry["depth_source"] = src
    entry["depth_support"] = mp.get("depth_support") if mp.get("depth_support") is not None else dd.get("support")
    entry["depth_diag_reason"] = dd.get("reason")
    entry["depth_diag"] = dd if dd else None
    entry["rgb_depth_fallback"] = bool(mp.get("rgb_depth_fallback"))
    entry["reachable"] = mp.get("reachable")
    entry["reach_m"] = mp.get("reach_m")
    if det is not None:
        entry["object_detection"] = det
    elif isinstance(mp.get("object_detection"), dict):
        entry["object_detection"] = mp.get("object_detection")


def _execute_autonomous_grasp(
    *,
    instruction: str,
    color_hint: str | None,
    max_cycles: int,
    use_supervisor: bool,
    on_progress: Any | None = None,
    seed_metric_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Esegue loop presa (senza job globale). Ritorna esito + log cicli."""
    import sys

    s = str(PROJECT_ROOT / "scripts")
    if s not in sys.path:
        sys.path.insert(0, s)
    from box_object_detector import parse_color_from_instruction

    from go2_dashboard import d1_arm_motion
    from go2_dashboard.d1_arm_publish_lite import (
        current_tool_tip_base_link_m,
        goto_tool_target_base_link_m_partial,
        publish_move_one_joint_deg,
    )
    from go2_dashboard.grasp_close_verify import verify_gripper_grasp
    from go2_dashboard.grasp_coach_agent import _coach_target_from_metric_plan, grasp_supervisor_review
    from go2_dashboard.grasp_online_calib import record_tcp_error
    from go2_dashboard.grasp_visual_servo import (
        apply_joint_deltas,
        visual_servo_metric,
        wrist_center_joint_deltas,
        wrist_extend_toward_object_deltas,
        _tolist,
    )
    from go2_dashboard.operator_stack import go2_local
    from go2_dashboard.orbbec_wrist_grasp import plan_wrist_grasp_metric

    hint = color_hint or parse_color_from_instruction(instruction)
    cycles_log: list[dict[str, Any]] = []
    ok_final = False
    grasp_verify: dict[str, Any] | None = None
    label_it = ""

    try:
        if not go2_local():
            return {
                "ok": False,
                "grasp_detected": False,
                "label_it": "GO2_LOCAL off — presa autonoma solo su NX.",
                "cycles": cycles_log,
            }
        if os.environ.get("GO2_ENABLE_REAL_ARM", "0").lower() not in {"1", "true", "yes", "on"}:
            return {
                "ok": False,
                "grasp_detected": False,
                "label_it": "GO2_ENABLE_REAL_ARM off.",
                "cycles": cycles_log,
            }

        sess = d1_arm_motion.ensure_grasp_motion_worker()
        if not (sess.get("ok") or sess.get("skipped")):
            return {
                "ok": False,
                "grasp_detected": False,
                "label_it": "Sessione braccio non avviata.",
                "cycles": cycles_log,
            }

        delay_ms = int(_envf("GO2_GRASP_COACH_DELAY_MS", 650))
        if os.environ.get("GO2_GRASP_TEACH_FAST", "1").lower() in {"1", "true", "yes", "on"}:
            delay_ms = int(_envf("GO2_GRASP_TEACH_FAST_DELAY_MS", 280))
        delay_ms = max(200, min(delay_ms, 2500))
        blend = _envf("GO2_GRASP_COACH_MAX_APPROACH_BLEND", 0.32)
        close_dist = _envf("GO2_GRASP_COACH_AUTOCLOSE_DIST_M", 0.04)
        tcp_tol = _envf("GO2_GRASP_COACH_TCP_TOL_M", 0.03)
        close_deg = _envf("GO2_GRASP_COACH_GRIPPER_CLOSE_DEG", -14.0)
        fast_loop = os.environ.get("GO2_GRASP_LOOP_FAST_CAPTURE", "1").lower() in {"1", "true", "yes", "on"}
        poll_s = _envf("GO2_GRASP_AUTONOMOUS_POLL_S", 0.55)
        if fast_loop:
            poll_s = min(poll_s, _envf("GO2_GRASP_FAST_POLL_S", 0.28))

        for cycle in range(max(1, max_cycles)):
            label_it = f"Ciclo {cycle + 1}/{max_cycles} — acquisizione metrica…"
            if callable(on_progress):
                on_progress(label_it=label_it, current_step=f"cycle_{cycle + 1}")
            servo = _servo_deg7()
            if servo is None:
                cycles_log.append({"step": "no_servo", "ok": False})
                break

            use_fast_cap = fast_loop and cycle > 0
            mp: dict[str, Any] | None = None
            if (
                cycle == 0
                and isinstance(seed_metric_plan, dict)
                and isinstance(seed_metric_plan.get("object_detection"), dict)
                and seed_metric_plan["object_detection"].get("ok")
            ):
                mp = seed_metric_plan
            if mp is None:
                mp = plan_wrist_grasp_metric(
                    servo,
                    instruction=instruction,
                    color_hint=hint,
                    fast_capture=use_fast_cap,
                )
            det = mp.get("object_detection") if isinstance(mp.get("object_detection"), dict) else {}
            partial_rgb = bool(det.get("ok")) and (
                bool(mp.get("partial_rgb_ok")) or bool(mp.get("rgb_depth_fallback"))
            )
            step_entry: dict[str, Any] = {
                "step": f"cycle_{cycle + 1}",
                "metric_ok": bool(mp.get("ok")),
                "reason": mp.get("reason"),
                "rgb_depth_fallback": bool(mp.get("rgb_depth_fallback")),
            }
            _attach_metric_depth_fields(step_entry, mp, det)
            if cycle == 0 and isinstance(seed_metric_plan, dict) and mp is seed_metric_plan:
                step_entry["seed_from_gates"] = True
            # Senza depth metrica reale: solo centraggio bbox + estensione (mai IK 3D verso target falso).
            if not mp.get("ok") and partial_rgb:
                fs = _tolist(det.get("frame_size_px"), [640, 480]) or [640, 480]
                frame_hw = (
                    float(fs[1]) if len(fs) > 1 else 480.0,
                    float(fs[0]) if len(fs) > 0 else 640.0,
                )
                center = wrist_center_joint_deltas(det, frame_hw, servo)
                if apply_joint_deltas(servo, (center or {}).get("joint_deltas_deg"), publish_move_one_joint_deg):
                    time.sleep(0.35)
                    servo = _servo_deg7() or servo
                    step_entry["visual_servo_applied"] = True
                extend = wrist_extend_toward_object_deltas(det, frame_hw, servo, rgb_fallback=True)
                if apply_joint_deltas(servo, (extend or {}).get("joint_deltas_deg"), publish_move_one_joint_deg):
                    time.sleep(0.4)
                    servo = _servo_deg7() or servo
                    step_entry["rgb_approach_applied"] = True
                    step_entry["rgb_approach"] = extend
                step_entry["reason"] = "rgb_approach_no_depth"
                # #region agent log
                _dbg_agent_log(
                    "grasp_autonomous_loop.py:rgb_only",
                    "RGB-only cycle (no IK 3D)",
                    {
                        "cycle": cycle + 1,
                        "reason": step_entry.get("reason"),
                        "rgb_depth_fallback": bool(mp.get("rgb_depth_fallback")),
                        "bbox": det.get("bbox_xyxy"),
                        "center": center,
                        "extend": extend,
                    },
                    "H1",
                )
                # #endregion
                cycles_log.append(step_entry)
                if callable(on_progress):
                    on_progress(cycle=step_entry)
                time.sleep(0.5)
                continue

            fs = _tolist(det.get("frame_size_px"), [640, 480]) or [640, 480]
            frame_hw = (
                float(fs[1]) if len(fs) > 1 else 480.0,
                float(fs[0]) if len(fs) > 0 else 640.0,
            )
            vs_before = visual_servo_metric(det, frame_hw)
            step_entry["visual_servo"] = vs_before

            if use_supervisor:
                sup = grasp_supervisor_review(
                    instruction=instruction,
                    metric_plan=mp,
                    color_hint=hint,
                )
                step_entry["supervisor"] = sup
                if sup.get("ok") and not sup.get("skipped") and not sup.get("approve"):
                    cycles_log.append(step_entry)
                    if callable(on_progress):
                        on_progress(cycle=step_entry)
                    label_it = f"Supervisor veto: {sup.get('reason_it', '')}"
                    time.sleep(0.8)
                    continue
                sb = sup.get("suggested_blend")
                if sb is not None:
                    try:
                        blend = max(0.08, min(0.4, float(sb)))
                    except (TypeError, ValueError):
                        pass

            step_entry["depth_m"] = mp.get("depth_m")
            step_entry["reachable"] = mp.get("reachable")
            step_entry["reach_m"] = mp.get("reach_m")

            center = wrist_center_joint_deltas(det, frame_hw, servo)
            if apply_joint_deltas(servo, (center or {}).get("joint_deltas_deg"), publish_move_one_joint_deg):
                time.sleep(0.35)
                servo = _servo_deg7() or servo
                step_entry["visual_servo_applied"] = True

            rgb_fb = bool(mp.get("rgb_depth_fallback"))
            extend = wrist_extend_toward_object_deltas(det, frame_hw, servo, rgb_fallback=rgb_fb)
            if apply_joint_deltas(servo, (extend or {}).get("joint_deltas_deg"), publish_move_one_joint_deg):
                time.sleep(0.4)
                servo = _servo_deg7() or servo
                step_entry["rgb_approach_applied"] = True
                step_entry["rgb_approach"] = extend

            cycle_blend = blend
            if rgb_fb:
                cycle_blend = max(blend, _envf("GO2_WRIST_RGB_FALLBACK_APPROACH_BLEND", 0.38))

            cur_tip, _ = current_tool_tip_base_link_m()
            depth_m = mp.get("depth_m")
            try:
                depth_val = float(depth_m) if depth_m is not None else None
            except (TypeError, ValueError):
                depth_val = None
            from go2_dashboard.orbbec_wrist_grasp import depth_plausible_m

            metric_unreachable = mp.get("reachable") is False or not depth_plausible_m(depth_val)
            if metric_unreachable and not rgb_fb:
                step_entry["reason"] = "metric_unreachable"
                step_entry["depth_m"] = depth_val
                step_entry["reach_m"] = mp.get("reach_m")
                cycles_log.append(step_entry)
                if callable(on_progress):
                    on_progress(cycle=step_entry)
                time.sleep(max(0.2, poll_s))
                continue

            tgt, stage = _coach_target_from_metric_plan(mp, cur_tip, lateral=True)
            grasp_disp = mp.get("grasp_display_base_link_m")
            max_coach_m = _envf("GO2_GRASP_MAX_COACH_TARGET_M", 0.55)
            coach_dist = None
            if (
                isinstance(grasp_disp, (list, tuple))
                and len(grasp_disp) >= 3
                and isinstance(cur_tip, (list, tuple))
                and len(cur_tip) >= 3
            ):
                coach_dist = math.sqrt(
                    sum((float(cur_tip[i]) - float(grasp_disp[i])) ** 2 for i in range(3))
                )
            stage_dist = None
            if (
                isinstance(tgt, (list, tuple))
                and len(tgt) >= 3
                and isinstance(cur_tip, (list, tuple))
                and len(cur_tip) >= 3
            ):
                stage_dist = math.sqrt(
                    sum((float(cur_tip[i]) - float(tgt[i])) ** 2 for i in range(3))
                )
            min_target_z = _envf("GO2_GRASP_MIN_EXEC_TARGET_Z_BASE_LINK_M", 0.05)
            target_z_bad = (
                isinstance(tgt, (list, tuple))
                and len(tgt) >= 3
                and float(tgt[2]) < min_target_z
            )
            final_grasp_z_bad = (
                stage == "grasp"
                and isinstance(grasp_disp, (list, tuple))
                and len(grasp_disp) >= 3
                and float(grasp_disp[2]) < min_target_z
            )
            sanity_fail = (
                (coach_dist is not None and coach_dist > max_coach_m)
                or (stage_dist is not None and stage_dist > max_coach_m)
                or target_z_bad
                or final_grasp_z_bad
                or metric_unreachable
            )
            if sanity_fail:
                step_entry["reason"] = "coach_target_sanity_fail"
                step_entry["coach_dist_m"] = round(coach_dist, 4) if coach_dist is not None else None
                step_entry["grasp_display"] = grasp_disp
                _dbg_agent_log(
                    "grasp_autonomous_loop.py:cycle",
                    "coach_target_sanity_fail skip IK",
                    {
                        "cycle": cycle + 1,
                        "coach_dist_m": coach_dist,
                        "stage": stage,
                        "stage_target": tgt,
                        "stage_dist_m": stage_dist,
                        "grasp_display": grasp_disp,
                        "depth_m": mp.get("depth_m"),
                        "min_target_z_m": min_target_z,
                    },
                    "H2",
                )
                cycles_log.append(step_entry)
                if callable(on_progress):
                    on_progress(cycle=step_entry)
                time.sleep(0.5)
                continue
            if tgt is None:
                step_entry["ok"] = False
                step_entry["reason"] = "no_coach_target"
                cycles_log.append(step_entry)
                _dbg_agent_log(
                    "grasp_autonomous_loop.py:cycle",
                    "no_coach_target continue",
                    {
                        "cycle": cycle + 1,
                        "depth_m": mp.get("depth_m"),
                        "reachable": mp.get("reachable"),
                        "reach_m": mp.get("reach_m"),
                        "grasp_display": mp.get("grasp_display_base_link_m"),
                    },
                    "H7",
                )
                if callable(on_progress):
                    on_progress(cycle=step_entry)
                poll_s = _envf("GO2_GRASP_AUTONOMOUS_POLL_S", 0.55)
                time.sleep(max(0.2, poll_s))
                continue

            tip_before, _ = current_tool_tip_base_link_m()
            if callable(on_progress):
                on_progress(
                    label_it=f"Ciclo {cycle + 1}/{max_cycles} — movimento verso target ({stage})…",
                    current_step=f"cycle_{cycle + 1}",
                )
            motion = dict(goto_tool_target_base_link_m_partial(tgt, approach_blend=cycle_blend, delay_ms=delay_ms))
            step_entry["motion"] = motion
            step_entry["coach_stage"] = stage
            step_entry["target"] = tgt
            _dbg_agent_log(
                "grasp_autonomous_loop.py:cycle",
                "coach_motion",
                {
                    "cycle": cycle + 1,
                    "stage": stage,
                    "target": tgt,
                    "motion_ok": motion.get("ok"),
                    "depth_m": mp.get("depth_m"),
                },
                "H8",
            )

            tip_after, _ = current_tool_tip_base_link_m()
            wp = motion.get("waypoint_base_link_m")
            if isinstance(wp, (list, tuple)) and len(wp) >= 3 and isinstance(tip_after, (list, tuple)):
                err_vec = [float(tip_after[i]) - float(wp[i]) for i in range(3)]
                err_norm = math.sqrt(sum(e * e for e in err_vec))
                motion["tcp_reach_error_m"] = round(err_norm, 4)
                motion["tcp_reach_ok"] = bool(err_norm <= tcp_tol)
                record_tcp_error([float(wp[i]) for i in range(3)], [float(tip_after[i]) for i in range(3)])

            grasp_final = mp.get("grasp_display_base_link_m")
            near_grasp = False
            if isinstance(grasp_final, (list, tuple)) and isinstance(tip_after, (list, tuple)):
                d = math.sqrt(sum((float(tip_after[i]) - float(grasp_final[i])) ** 2 for i in range(3)))
                step_entry["dist_to_grasp_m"] = round(d, 4)
                near_grasp = d <= close_dist

            auto_close = bool(
                motion.get("ok")
                and (stage == "grasp" or near_grasp)
                and motion.get("tcp_reach_ok") is not False
            )
            if auto_close:
                grip_out = publish_move_one_joint_deg(6, close_deg)
                step_entry["gripper_close"] = grip_out
                grasp_verify = verify_gripper_grasp(close_deg, hold_s=0.65)
                step_entry["grasp_verify"] = grasp_verify
                cycles_log.append(step_entry)
                if callable(on_progress):
                    on_progress(cycle=step_entry)
                ok_final = bool(grasp_verify.get("grasp_detected"))
                break

            cycles_log.append(step_entry)
            if callable(on_progress):
                on_progress(cycle=step_entry)
            poll_s = _envf("GO2_GRASP_AUTONOMOUS_POLL_S", 0.55)
            time.sleep(max(0.2, poll_s))

        label_it = (
            "Presa autonoma OK — oggetto in pinza."
            if ok_final
            else "Presa autonoma terminata senza verify positivo."
        )
        return {
            "ok": ok_final,
            "grasp_detected": ok_final,
            "grasp_verify": grasp_verify,
            "cycles": cycles_log,
            "label_it": label_it,
        }
    except Exception as exc:
        import traceback

        _dbg_agent_log(
            "grasp_autonomous_loop.py:exception",
            "autonomous_grasp_exception",
            {"error": repr(exc), "traceback": traceback.format_exc()[-1200:]},
            "H9",
        )
        return {
            "ok": False,
            "grasp_detected": False,
            "label_it": f"Errore presa autonoma: {exc!r}",
            "cycles": cycles_log,
        }


def _run_autonomous_worker(
    *,
    instruction: str,
    color_hint: str | None,
    max_cycles: int,
    use_supervisor: bool,
) -> None:
    def _on_progress(**kw: Any) -> None:
        if "label_it" in kw:
            _job_set(label_it=kw["label_it"], current_step=kw.get("current_step"))
        if "cycle" in kw:
            _job_cycle(kw["cycle"])

    result = _execute_autonomous_grasp(
        instruction=instruction,
        color_hint=color_hint,
        max_cycles=max_cycles,
        use_supervisor=use_supervisor,
        on_progress=_on_progress,
    )
    try:
        log_path = PROJECT_ROOT / "data" / "grasp_autonomous_last.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    _job_set(
        running=False,
        ok=bool(result.get("grasp_detected")),
        finished_at=_now_iso(),
        cycles=result.get("cycles") or [],
        grasp_verify=result.get("grasp_verify"),
        label_it=result.get("label_it") or "",
    )
    try:
        log_path = PROJECT_ROOT / "data" / "grasp_autonomous_last.json"
        log_path.write_text(
            json.dumps(autonomous_grasp_status(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def start_autonomous_grasp(
    *,
    instruction: str = "",
    confirm: str | None = None,
    color_hint: str | None = None,
    max_cycles: int | None = None,
    use_supervisor: bool | None = None,
) -> tuple[dict[str, Any], int]:
    with _JOB_LOCK:
        if _JOB.get("running"):
            return (
                {"ok": False, "reason": "job_already_running", "status": autonomous_grasp_status()},
                409,
            )

    dry = confirm != CONFIRM_TOKEN
    mc = max_cycles if max_cycles is not None else int(_envf("GO2_GRASP_AUTONOMOUS_MAX_CYCLES", 24))
    mc = max(1, min(mc, 60))
    sup = use_supervisor if use_supervisor is not None else _truthy("GO2_GRASP_COACH_SUPERVISOR", "1")

    if dry:
        return (
            {
                "ok": True,
                "started": False,
                "dry_run": True,
                "confirm_required": CONFIRM_TOKEN,
                "max_cycles": mc,
                "use_supervisor": sup,
                "hint_it": f"Dry-run — ripeti con confirm={CONFIRM_TOKEN!r} per eseguire.",
            },
            200,
        )

    _job_set(
        running=True,
        ok=None,
        started_at=_now_iso(),
        finished_at=None,
        current_step="starting",
        label_it="Avvio presa autonoma…",
        cycles=[],
        grasp_verify=None,
        params={
            "instruction": instruction,
            "color_hint": color_hint,
            "max_cycles": mc,
            "use_supervisor": sup,
        },
    )
    th = threading.Thread(
        target=_run_autonomous_worker,
        kwargs={
            "instruction": instruction or "prendi la scatola",
            "color_hint": color_hint,
            "max_cycles": mc,
            "use_supervisor": sup,
        },
        name="grasp_autonomous",
        daemon=True,
    )
    th.start()
    return (
        {
            "ok": True,
            "started": True,
            "poll": "/api/grasp/autonomous_status",
            "status": autonomous_grasp_status(),
        },
        202,
    )

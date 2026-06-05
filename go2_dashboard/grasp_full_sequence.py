"""Sequenza presa completa (un solo comando): frontale vede l'oggetto → braccio a START
→ pose estimation dal polso → presa a fasi sul D1.

Concatena i mattoni già esistenti:
  1. ``front_detect``  — box_object_detector sulla camera frontale (logical 6) per confermare
     che l'oggetto è inquadrato prima di muovere il braccio.
  2. ``goto_start``    — ``goto_saved_start_from_json`` porta il braccio alla posa START salvata
     (da lì il polso vede l'oggetto che è dietro al corpo del cane).
  3. ``wrist_plan``    — ``grasp_plan_via_worker`` con ``logical_camera_device=0`` (polso): il worker
     AWS/locale calcola target 3D + preview IK (pose estimation reale).
  4. ``execute_phased``— ``execute_phased_from_cached_plan`` esegue pre_grasp→approach→grasp→lift.

I passi che muovono il braccio (2 e 4) girano **solo** con ``confirm="RUN_FULL_GRASP"``; senza
confirm la sequenza è un dry-run (detect + piano dalla posa corrente) per anteprima/overlay.
I gate hardware (``GO2_LOCAL`` / ``GO2_ENABLE_REAL_ARM`` / flag plan-execute) restano quelli dei
mattoni sottostanti: questo modulo non li bypassa.
"""

from __future__ import annotations

import os
import time
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT

CONFIRM_TOKEN = "RUN_FULL_GRASP"


def _truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _settle_ms() -> int:
    try:
        return max(0, int((os.environ.get("GO2_GRASP_FULL_SETTLE_MS") or "900").strip()))
    except ValueError:
        return 900


def _detect_on_camera(camera: int) -> dict[str, Any]:
    """Detection NX-side sul frame logico ``camera`` con il detector buono (floor_object_saliency).

    Restituisce ``{"ok": bool, "detection": {...}|None, "reason": str}``. La detection include
    ``frame_size_px``/``logical_camera`` per scalare correttamente bbox e profondità lato worker.
    """
    import sys

    import cv2
    import numpy as np

    from go2_dashboard.cameras import CAMERA_CACHE
    from go2_dashboard.paths import PROJECT_ROOT

    CAMERA_CACHE.start(camera)
    jpg = CAMERA_CACHE.get_jpeg(camera, wait_s=2.5)
    if not jpg:
        return {"ok": False, "detection": None, "reason": "no_frame"}
    frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return {"ok": False, "detection": None, "reason": "jpeg_decode_failed"}
    s_scripts = str(PROJECT_ROOT / "scripts")
    if s_scripts not in sys.path:
        sys.path.insert(0, s_scripts)
    from box_object_detector import detect_box_object

    det = detect_box_object(frame)
    if isinstance(det, dict):
        det.setdefault("frame_size_px", [int(frame.shape[1]), int(frame.shape[0])])
        det.setdefault("logical_camera", camera)
    ok = bool(isinstance(det, dict) and det.get("ok"))
    debug_tag = "front" if int(camera) == 6 else ("wrist" if int(camera) == 0 else f"cam{int(camera)}")
    try:
        from go2_dashboard.grasp_detect_debug import save_detection_snapshot

        dbg = save_detection_snapshot(
            frame,
            det if isinstance(det, dict) else None,
            tag=debug_tag,
            logical_camera=int(camera),
            step="detect_on_camera",
        )
    except Exception as exc:
        dbg = {"saved": False, "error": repr(exc)}
    return {
        "ok": ok,
        "detection": det if isinstance(det, dict) else None,
        "reason": (det or {}).get("reason", ""),
        "debug_snapshot": dbg,
    }


def _front_detect(camera: int, instruction: str) -> dict[str, Any]:
    """Detector sulla camera frontale (cache dashboard). Non fatale: serve solo a confermare l'oggetto."""
    step: dict[str, Any] = {"step": "front_detect", "camera": camera, "ok": False}
    try:
        from go2_dashboard.operator_stack import go2_local
    except Exception as exc:  # pragma: no cover - import guard
        step["reason"] = "operator_stack_unavailable"
        step["detail"] = repr(exc)
        return step
    if not go2_local():
        step["ok"] = True
        step["skipped"] = True
        step["reason"] = "go2_local_off"
        step["hint_it"] = "Detector frontale saltato (GO2_LOCAL!=1): la sequenza userà solo il piano dal polso."
        return step
    try:
        r = _detect_on_camera(camera)
        det = r.get("detection")
        if isinstance(det, dict) and instruction:
            det["instruction"] = instruction
        step["detection"] = det
        step["debug_snapshot"] = r.get("debug_snapshot")
        step["ok"] = bool(r.get("ok"))
        if step["ok"]:
            step["label_it"] = "Oggetto inquadrato dalla frontale"
            step["object_backend"] = (det or {}).get("backend")
            step["object_confidence"] = (det or {}).get("confidence")
        else:
            step["reason"] = r.get("reason") or "no_box_detection"
            step["hint_it"] = "La frontale non rileva l'oggetto — riposiziona il cane o l'oggetto e riprova."
    except Exception as exc:
        step["reason"] = "front_detect_exception"
        step["detail"] = repr(exc)
    return step


def _check_start_pose() -> dict[str, Any]:
    """Verifica che il braccio sia sulla START salvata (senza muovere)."""
    step: dict[str, Any] = {"step": "start_pose_check", "ok": False}
    try:
        from go2_dashboard.d1_arm_publish_lite import check_at_saved_start_pose

        chk = check_at_saved_start_pose()
        step["at_start_check"] = chk
        step["ok"] = bool(chk.get("ok"))
        step["max_error_deg"] = chk.get("max_error_deg")
        step["delta_deg_7"] = chk.get("delta_deg_7")
        if step["ok"]:
            step["label_it"] = f"In posa START (max err {chk.get('max_error_deg')}°)"
        else:
            step["reason"] = "not_at_saved_start"
            step["hint_it"] = chk.get("hint_it") or "Vai a START prima del piano/presa."
    except Exception as exc:
        step["reason"] = "start_pose_check_exception"
        step["detail"] = repr(exc)
    return step


def _goto_start() -> dict[str, Any]:
    """Fold (opz.) → goto START file → verifica feedback vs start_alignment.json."""
    step: dict[str, Any] = {"step": "goto_start", "ok": False}
    try:
        from go2_dashboard import d1_arm_motion
        from go2_dashboard.d1_arm_publish_lite import (
            _use_operator_arm_motion,
            check_at_saved_start_pose,
            goto_fold_compact_for_grasp,
            goto_saved_start_from_json,
        )
        from go2_dashboard.operator_arm_motion import hold_operator_arm_pose

        step["motion_worker"] = d1_arm_motion.ensure_grasp_motion_worker()
        if not step["motion_worker"].get("ok"):
            step["reason"] = step["motion_worker"].get("reason") or "motion_worker_failed"
            step["hint_it"] = (
                "Daemon DDS braccio non pronto — verifica bin/d1_sdk_command e Coppia (come tab jog 5053)."
            )
            return step

        pre_chk = check_at_saved_start_pose()
        step["pre_start_check"] = pre_chk
        if pre_chk.get("ok"):
            step["ok"] = True
            step["skipped_motion"] = True
            step["label_it"] = "Già in START salvata — fold/goto saltati"
            return step

        # Sessione live + tick DDS come tab Giunti (live_deg subito dopo session_begin).
        fold = goto_fold_compact_for_grasp()
        step["goto_fold"] = fold
        if not fold.get("ok") and not fold.get("skipped"):
            step["reason"] = fold.get("reason") or "goto_fold_failed"
            step["hint_it"] = "Fold verso posa compatta fallito — vedi goto_fold."
            return step
        if not fold.get("skipped"):
            time.sleep(max(0, _env_int("GO2_GRASP_FOLD_SETTLE_MS", 400)) / 1000.0)

        def _align_to_start_file() -> dict[str, Any]:
            out_align = goto_saved_start_from_json()
            time.sleep(max(0.3, _env_int("GO2_GRASP_START_SETTLE_MS", 900)) / 1000.0)
            if _use_operator_arm_motion():
                hold_operator_arm_pose()
            time.sleep(0.25)
            return out_align

        out = _align_to_start_file()
        step["goto_saved_start"] = out
        if not out.get("ok"):
            step["reason"] = out.get("reason")
            step["hint_it"] = out.get("hint_it") or "goto_saved_start fallito — vedi result."
            return step

        chk = check_at_saved_start_pose()
        if not chk.get("ok") and _truthy("GO2_GRASP_START_ALIGN_RETRY", "1"):
            step["goto_saved_start_retry"] = _align_to_start_file()
            chk = check_at_saved_start_pose()

        step["post_start_hold"] = hold_operator_arm_pose() if _use_operator_arm_motion() else None
        step["at_start_check"] = chk
        step["ok"] = bool(chk.get("ok"))
        if step["ok"]:
            step["label_it"] = "Fold + START raggiunta (polso verso l'oggetto)"
        else:
            step["reason"] = "start_align_verify_failed"
            step["hint_it"] = chk.get("hint_it")
    except Exception as exc:
        step["reason"] = "goto_start_exception"
        step["detail"] = repr(exc)
    return step


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or str(default)).strip())
    except ValueError:
        return default


def _current_servo_deg7() -> list[float] | None:
    """Feedback servo (7 valori, gradi) per FK/IK del path metrico. None se non disponibile."""
    try:
        from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag
        from go2_dashboard.paths import PROJECT_ROOT

        angles, _diag = read_servo_deg_with_diag(PROJECT_ROOT)
        if angles is not None and len(angles) >= 6:
            out = [float(angles[i]) for i in range(min(7, len(angles)))]
            while len(out) < 7:
                out.append(0.0)
            return out
    except Exception:
        return None
    return None


def _wrist_plan_metric(instruction: str, wrist_camera: int) -> dict[str, Any] | None:
    """Path metrico NX (Orbbec SDK): depth reale → base_link via FK → reach → IK.

    Ritorna lo ``step`` se il path metrico è applicabile e ha prodotto un piano (anche bloccato
    per reach/IK), altrimenti ``None`` per ricadere sul worker.
    """
    if int(wrist_camera) != 0 or _truthy("GO2_WRIST_METRIC_DISABLE", "0"):
        return None
    try:
        from go2_dashboard.operator_stack import go2_local

        if not go2_local():
            return None
        from go2_dashboard import orbbec_wrist_grasp as owg

        if not owg.available():
            return None
        servo = _current_servo_deg7()
        if servo is None:
            return None
        mp = owg.plan_wrist_grasp_metric(servo, instruction=instruction)
        if not mp.get("ok"):
            return {
                "step": "wrist_plan",
                "camera": wrist_camera,
                "ok": False,
                "metric_path": True,
                "plan": mp,
                "reason": mp.get("reason"),
                "hint_it": (
                    "Orbbec metrico: "
                    + str(mp.get("reason") or "piano non ok")
                    + " — vedi /api/grasp/detection_debug/wrist_orbbec.jpg"
                ),
                "wrist_detection": mp.get("object_detection"),
                "debug_snapshot": mp.get("debug_snapshot"),
                "depth_diag": mp.get("depth_diag"),
            }
        step: dict[str, Any] = {"step": "wrist_plan", "camera": wrist_camera, "ok": True}
        step["metric_path"] = True
        step["plan"] = mp
        step["backend"] = mp.get("backend")
        step["validation_ui"] = mp.get("validation_ui")
        step["grasp_assessment"] = mp.get("grasp_assessment")
        step["wrist_detection"] = mp.get("object_detection")
        step["wrist_detection_used"] = True
        step["reach_m"] = mp.get("reach_m")
        step["reachable"] = mp.get("reachable")
        ass = mp.get("grasp_assessment") or {}
        step["label_it"] = "Pose estimation metrica (Orbbec) · " + str(ass.get("label_it") or "piano")
        try:
            from go2_dashboard.operator_plan_cache import set_last_grasp_plan

            set_last_grasp_plan(mp)
        except Exception as exc:
            step["plan_cache_error"] = repr(exc)
        return step
    except Exception:
        return None


def _wrist_plan_graspgen_metric(instruction: str, wrist_camera: int) -> dict[str, Any] | None:
    """Ponte metrico→GraspGen (primario): nuvola metrica Orbbec → server GraspGen → 6-DoF → IK D1.

    Ritorna lo ``step`` solo se ha prodotto un piano valido; ``None`` per ricadere sul path metrico
    euristico locale (e poi sul worker). Disattivabile con ``GO2_WRIST_GRASPGEN_FIRST=0``.
    """
    if int(wrist_camera) != 0 or not _truthy("GO2_WRIST_GRASPGEN_FIRST", "1"):
        return None
    try:
        from go2_dashboard.operator_stack import go2_local

        if not go2_local():
            return None
        from go2_dashboard import orbbec_wrist_grasp as owg

        if not owg.available():
            return None
        servo = _current_servo_deg7()
        if servo is None:
            return None
        gp = owg.plan_wrist_grasp_graspgen(servo, instruction=instruction)
        if not gp.get("ok"):
            # Fallback morbido al path metrico locale: non blocchiamo la sequenza.
            return None
        step: dict[str, Any] = {"step": "wrist_plan", "camera": wrist_camera, "ok": True}
        step["metric_path"] = True
        step["graspgen_path"] = True
        step["plan"] = gp
        step["backend"] = gp.get("backend")
        step["validation_ui"] = gp.get("validation_ui")
        step["grasp_assessment"] = gp.get("grasp_assessment")
        step["wrist_detection"] = gp.get("object_detection")
        step["wrist_detection_used"] = True
        step["reach_m"] = gp.get("reach_m")
        step["reachable"] = gp.get("reachable")
        step["graspgen"] = gp.get("graspgen")
        ass = gp.get("grasp_assessment") or {}
        step["label_it"] = "GraspGen 6-DoF (nuvola metrica Orbbec) · " + str(ass.get("label_it") or "piano")
        try:
            from go2_dashboard.operator_plan_cache import set_last_grasp_plan

            set_last_grasp_plan(gp)
        except Exception as exc:
            step["plan_cache_error"] = repr(exc)
        return step
    except Exception:
        return None


def _wrist_plan(instruction: str, wrist_camera: int) -> dict[str, Any]:
    graspgen = _wrist_plan_graspgen_metric(instruction, wrist_camera)
    if graspgen is not None:
        return graspgen

    metric = _wrist_plan_metric(instruction, wrist_camera)
    if metric is not None:
        return metric

    step: dict[str, Any] = {"step": "wrist_plan", "camera": wrist_camera, "ok": False}
    try:
        from go2_dashboard.blueprints.grasp import grasp_plan_via_worker

        body = {"instruction": instruction, "logical_camera_device": int(wrist_camera)}

        # Detection NX-side sul frame del polso: il worker la usa come ROI per la profondità
        # (così il 3D è sull'oggetto, non sulla mediana di tutto il frame). Disattivabile via env.
        if not _truthy("GO2_GRASP_WRIST_DETECT_DISABLE", "0"):
            try:
                from go2_dashboard.operator_stack import go2_local

                if go2_local():
                    wd = _detect_on_camera(int(wrist_camera))
                    step["wrist_detection"] = wd.get("detection")
                    if wd.get("ok") and isinstance(wd.get("detection"), dict):
                        det = dict(wd["detection"])
                        if instruction:
                            det["instruction"] = instruction
                        body["object_detection"] = det
                        step["wrist_detection_used"] = True
                    else:
                        step["wrist_detection_used"] = False
                        step["wrist_detection_reason"] = wd.get("reason")
            except Exception as exc:  # detection non fatale: il worker farà fallback da sé
                step["wrist_detection_error"] = repr(exc)

        payload, code = grasp_plan_via_worker(body)
        step["http_code"] = code
        step["plan"] = payload
        ok = bool(isinstance(payload, dict) and payload.get("ok") and code < 400)
        step["ok"] = ok
        if isinstance(payload, dict):
            step["validation_ui"] = payload.get("validation_ui")
            step["grasp_assessment"] = payload.get("grasp_assessment")
            step["backend"] = payload.get("backend")
        if ok:
            ass = (payload.get("grasp_assessment") or {}) if isinstance(payload, dict) else {}
            step["label_it"] = "Pose estimation dal polso · " + str(ass.get("label_it") or "piano pronto")
        else:
            step["reason"] = (payload or {}).get("reason", "plan_failed")
            step["hint_it"] = (payload or {}).get("hint_it") or "Piano dal polso non valido."
    except Exception as exc:
        step["reason"] = "wrist_plan_exception"
        step["detail"] = repr(exc)
    return step


def _execute_phased(allow_heuristic: bool) -> dict[str, Any]:
    step: dict[str, Any] = {"step": "execute_phased", "ok": False}
    try:
        from go2_dashboard.grasp_phased_execute import execute_phased_from_cached_plan

        out = execute_phased_from_cached_plan(
            confirm="EXECUTE_PHASED_GRASP",
            allow_heuristic_override=allow_heuristic,
        )
        step["ok"] = bool(out.get("ok"))
        step["result"] = out
        step["stages_run"] = out.get("stages_run")
        if step["ok"]:
            step["label_it"] = f"Presa eseguita ({out.get('stages_run', '?')} fasi)"
        else:
            step["reason"] = out.get("reason")
            step["hint_it"] = out.get("hint_it")
    except Exception as exc:
        step["reason"] = "execute_phased_exception"
        step["detail"] = repr(exc)
    return step


def run_full_grasp_sequence(
    *,
    instruction: str,
    confirm: str | None = None,
    front_camera: int = 6,
    wrist_camera: int = 0,
    do_goto_start: bool = True,
    do_execute: bool = True,
    allow_heuristic_override: bool | None = None,
) -> dict[str, Any]:
    """Orchestratore della presa completa. Vedi docstring modulo per il flusso e i gate confirm."""
    instruction = (instruction or "").strip() or "afferra l'oggetto davanti al braccio"
    move_allowed = confirm == CONFIRM_TOKEN
    allow_heur = allow_heuristic_override
    if allow_heur is None:
        allow_heur = _truthy("GO2_GRASP_ALLOW_HEURISTIC_EXECUTE", "0")

    steps: list[dict[str, Any]] = []
    failed_step: str | None = None

    # 1. Frontale vede l'oggetto (informativo, non blocca la sequenza)
    front = _front_detect(front_camera, instruction)
    steps.append(front)

    # 2. Braccio a START (movimento solo con confirm; verifica sempre se richiesto)
    require_start = _truthy("GO2_GRASP_REQUIRE_START_BEFORE_PLAN", "1")
    if do_goto_start:
        if move_allowed:
            gs = _goto_start()
            steps.append(gs)
            if not gs.get("ok"):
                failed_step = "goto_start"
                return _result(instruction, steps, failed_step, move_allowed)
        else:
            steps.append(
                {
                    "step": "goto_start",
                    "ok": True,
                    "skipped": True,
                    "reason": "confirm_required",
                    "label_it": "START non eseguita (dry-run) — serve confirm RUN_FULL_GRASP per fold+START",
                }
            )
        if require_start:
            sp = _check_start_pose()
            steps.append(sp)
            if not sp.get("ok"):
                failed_step = "start_pose_check"
                return _result(instruction, steps, failed_step, move_allowed)

    # 3. Pose estimation dal polso (solo da START verificata)
    wp = _wrist_plan(instruction, wrist_camera)
    steps.append(wp)
    last_plan = wp.get("plan") if isinstance(wp.get("plan"), dict) else None
    if not wp.get("ok"):
        failed_step = "wrist_plan"
        return _result(instruction, steps, failed_step, move_allowed, plan=last_plan)

    # 4. Presa a fasi (solo con confirm)
    if do_execute:
        if move_allowed:
            ex = _execute_phased(bool(allow_heur))
            steps.append(ex)
            if not ex.get("ok"):
                failed_step = "execute_phased"
        else:
            steps.append(
                {
                    "step": "execute_phased",
                    "ok": True,
                    "skipped": True,
                    "reason": "confirm_required",
                    "label_it": "Presa saltata (dry-run) — invia confirm RUN_FULL_GRASP per muovere il D1",
                }
            )

    return _result(instruction, steps, failed_step, move_allowed, plan=last_plan)


def _result(
    instruction: str,
    steps: list[dict[str, Any]],
    failed_step: str | None,
    move_allowed: bool,
    *,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = plan if isinstance(plan, dict) else None
    out = {
        "ok": failed_step is None,
        "mode": "full_grasp" if move_allowed else "full_grasp_dry_run",
        "instruction": instruction,
        "confirm_required_token": CONFIRM_TOKEN,
        "moved_arm": move_allowed,
        "steps": steps,
        "failed_step": failed_step,
        "plan": plan,
        "validation_ui": (plan or {}).get("validation_ui"),
        "grasp_assessment": (plan or {}).get("grasp_assessment"),
    }
    try:
        from go2_dashboard.grasp_detect_debug import read_debug_manifest

        out["detection_debug"] = read_debug_manifest()
        out["detection_debug_urls"] = {
            "front_jpg": "/api/grasp/detection_debug/front.jpg",
            "wrist_jpg": "/api/grasp/detection_debug/wrist.jpg",
            "wrist_orbbec_jpg": "/api/grasp/detection_debug/wrist_orbbec.jpg",
            "manifest": "/api/grasp/detection_debug",
        }
    except Exception as exc:
        out["detection_debug_error"] = repr(exc)
    try:
        log_path = PROJECT_ROOT / "data" / "grasp_run_full_last.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        import json

        log_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        out["log_path"] = str(log_path)
    except Exception as exc:
        out["log_write_error"] = repr(exc)
    return out

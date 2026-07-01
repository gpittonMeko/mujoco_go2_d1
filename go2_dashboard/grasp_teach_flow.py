"""Flusso teach unificato: coppia → Scansione +90° → gate metrici → presa autonoma o raccolta."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT

CONFIRM_TOKEN = "RUN_TEACH_GRASP"


def _dbg_fdb211(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    # #region agent log
    try:
        p = PROJECT_ROOT / "debug-fdb211.log"
        with open(p, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "sessionId": "fdb211",
                        "hypothesisId": hypothesis_id,
                        "location": location,
                        "message": message,
                        "data": data or {},
                        "timestamp": int(time.time() * 1000),
                        "runId": "nx-teach",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass
    # #endregion

_COLLECT_RE = re.compile(r"(?i)\b(raccogli(?:ere)?|collect|pick\s+up\s+all|gather)\b")

_STEP_ORDER = ("couple", "scan_j90", "gates", "grasp", "verify", "done")

_STEP_LABELS: dict[str, str] = {
    "couple": "Coppia braccio attiva",
    "scan_j90": "Posa Scansione +90°",
    "gates": "Rilevamento e gate (depth · detect · reach)",
    "grasp": "Presa autonoma (stream polso)",
    "verify": "Verifica pinza",
    "done": "Completato",
}

_JOB_LOCK = threading.RLock()
_CANCEL = threading.Event()
_JOB: dict[str, Any] = {
    "running": False,
    "flow": "teach_flow",
    "ok": None,
    "mode": None,
    "started_at": None,
    "finished_at": None,
    "current_step": None,
    "failed_step": None,
    "label_it": "Nessun flusso teach avviato.",
    "progress_pct": 0,
    "steps": [],
    "log_lines": [],
    "params": None,
    "gates": None,
    "metric_plan": None,
    "live_wrist": None,
    "grasp_verify": None,
    "picks_done": 0,
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


def _fresh_steps() -> list[dict[str, Any]]:
    return [
        {"id": sid, "label_it": _STEP_LABELS[sid], "status": "idle", "detail": ""}
        for sid in _STEP_ORDER
    ]


def _snap_teach_to_disk() -> None:
    """Snapshot su disco — sopravvive a restart Flask e aiuta debug."""
    try:
        log_path = PROJECT_ROOT / "data" / "grasp_teach_flow_last.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(teach_flow_status(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def teach_flow_status() -> dict[str, Any]:
    with _JOB_LOCK:
        return json.loads(json.dumps({k: v for k, v in _JOB.items()}, default=str))


def _job_set(**kw: Any) -> None:
    with _JOB_LOCK:
        _JOB.update(kw)
    if kw.get("running") is not False:
        _snap_teach_to_disk()


def _set_step(step_id: str, status: str, *, detail: str = "") -> None:
    with _JOB_LOCK:
        steps = _JOB.get("steps") or []
        for s in steps:
            if s.get("id") == step_id:
                s["status"] = status
                if detail:
                    s["detail"] = detail
                break
        _JOB["steps"] = steps
        _JOB["current_step"] = step_id
    _snap_teach_to_disk()


def _append_log(level: str, step: str, msg_it: str) -> None:
    with _JOB_LOCK:
        lines = list(_JOB.get("log_lines") or [])
        lines.append({"ts": _now_iso(), "level": level, "step": step, "msg_it": msg_it})
        if len(lines) > 120:
            lines = lines[-120:]
        _JOB["log_lines"] = lines


def _progress(pct: float, label_it: str | None = None) -> None:
    kw: dict[str, Any] = {"progress_pct": max(0, min(100, int(pct)))}
    if label_it is not None:
        kw["label_it"] = label_it
    _job_set(**kw)


def _is_collect_mode(instruction: str) -> bool:
    return bool(_COLLECT_RE.search(instruction or ""))


def _teach_cancelled() -> bool:
    return _CANCEL.is_set()


def _teach_orbbec_steal_for_gates() -> dict[str, Any]:
    """Lock esclusivo Orbbec SDK prima dei gate metrici (legacy Orbbec polso)."""
    try:
        from go2_dashboard.d1_jog.orbbec_capture import steal_orbbec

        return steal_orbbec()
    except Exception as exc:
        return {"ok": False, "reason": "steal_exception", "detail": repr(exc)}


def _teach_wrist_camera_prepare() -> dict[str, Any]:
    """Prepara la camera polso per i gate metrici (RealSense: pausa V4L; Orbbec: lock SDK)."""
    from go2_dashboard.cameras import wrist_depth_backend

    if wrist_depth_backend() == "realsense":
        settle = float((os.environ.get("GO2_REALSENSE_CAPTURE_SETTLE_S") or "0.45").strip() or 0.45)
        try:
            from go2_dashboard.cameras import CAMERA_CACHE

            CAMERA_CACHE.request_pause(0, duration_s=settle + 0.25)
        except Exception:
            pass
        return {
            "ok": True,
            "backend": "realsense",
            "hint": "V4L log.0 in pausa per capture pyrealsense2",
        }
    return _teach_orbbec_steal_for_gates()


def _couple_fail_hint(reason: str) -> str:
    r = (reason or "").strip().lower()
    if r in {"busy", "motion_guard_busy"} or "busy" in r:
        return (
            f"{reason} — chiudi sessione live tab «Braccio D1 · giunti» "
            "o altra missione braccio in corso, poi riprova."
        )
    if r in {"not_coupled", "couple_failed"}:
        return f"{reason} — apri tab Giunti, verifica coppia DDS e daemon d1_sdk_command attivo."
    return reason or "coppia non attiva"


def _other_jobs_running() -> bool:
    from go2_dashboard.grasp_autonomous_loop import autonomous_grasp_status
    from go2_dashboard.grasp_collection_mission import collect_mission_status
    from go2_dashboard.grasp_side_approach import side_approach_status

    for st in (autonomous_grasp_status(), collect_mission_status(), side_approach_status()):
        if st.get("running"):
            return True
    return False


def _goto_scan_j90() -> dict[str, Any]:
    from go2_dashboard.d1_jog import program_store, program_runner, service
    from go2_dashboard.debug_agent_log import dbg_agent_log

    found = program_store.find_scan_waypoint(variant="j90")
    if found is None:
        dbg_agent_log(
            "grasp_teach_flow.py:_goto_scan_j90",
            "teach_scan_j90_missing",
            {"reason": "scan_j90_waypoint_not_found"},
            hypothesis_id="H-SCAN",
            run_id="teach-flow",
        )
        return {"ok": False, "reason": "scan_j90_waypoint_not_found"}
    _pid, wp = found
    raw = wp.get("servo_deg")
    if not isinstance(raw, list) or len(raw) < 6:
        return {"ok": False, "reason": "invalid_waypoint"}
    servo = service.clamp_servo_deg([float(x) for x in raw[:7]])
    service._halt_cartesian_stream(wait_idle=True)
    couple = service.ensure_coupled_for_motion()
    if not couple.get("ok"):
        return {"ok": False, "reason": "not_coupled", "coupling": couple}
    from go2_dashboard import d1_arm_motion

    keep_lock = bool(d1_arm_motion.is_live_session_active())
    out = program_runner.move_to_servo_deg_smooth(servo, keep_lock=keep_lock)
    if not out.get("ok") and str(out.get("reason") or "").startswith("plane_busy"):
        out["hint_it"] = (
            "Sessione braccio occupata — chiudi tab «Braccio D1 · giunti» o annulla flusso grasp."
        )
    out["scan_variant"] = "j90"
    out["waypoint_name"] = wp.get("name")
    out["target_servo_deg"] = servo
    dbg_agent_log(
        "grasp_teach_flow.py:_goto_scan_j90",
        "teach_scan_j90_result",
        {
            "ok": out.get("ok"),
            "reason": out.get("reason"),
            "waypoint_name": wp.get("name"),
            "max_error_deg": (out.get("wait_at_target") or {}).get("max_error_deg"),
        },
        hypothesis_id="H-SCAN",
        run_id="teach-flow",
    )
    return out


def _depth_source_label_it(source: str | None) -> str:
    labels = {
        "realsense_metric": "D456 metrica",
        "rgb_bbox_area": "stima RGB",
        "bbox_ring": "D456 anello bbox",
        "bbox_halo": "D456 alone bordi",
        "depth_implausible": "D456 scartata",
    }
    key = str(source or "").strip()
    return labels.get(key, key or "—")


def _wrist_live_from_metric(mp: dict[str, Any] | None, *, cycle: str | None = None) -> dict[str, Any]:
    """Snapshot depth/metrica polso per polling UI teach (RealSense D456)."""
    if not isinstance(mp, dict):
        return {}
    det = mp.get("object_detection") if isinstance(mp.get("object_detection"), dict) else {}
    dd = mp.get("depth_diag") if isinstance(mp.get("depth_diag"), dict) else {}
    src = mp.get("depth_source") or dd.get("reason")
    dm = mp.get("depth_m")
    rgb_fb = bool(mp.get("rgb_depth_fallback"))
    depth_ok = bool(mp.get("ok") and dm is not None and not rgb_fb)
    snap = mp.get("debug_snapshot") if isinstance(mp.get("debug_snapshot"), dict) else {}
    return {
        "cycle": cycle,
        "depth_m": dm,
        "depth_m_raw": mp.get("depth_m_raw"),
        "depth_source": src,
        "depth_source_it": _depth_source_label_it(str(src) if src else None),
        "depth_ok": depth_ok,
        "rgb_depth_fallback": rgb_fb,
        "depth_support": mp.get("depth_support") if mp.get("depth_support") is not None else dd.get("support"),
        "depth_diag_reason": dd.get("reason"),
        "depth_nonzero_px": mp.get("depth_nonzero_px"),
        "reachable": mp.get("reachable"),
        "reach_m": mp.get("reach_m"),
        "reason": mp.get("reason"),
        "confidence": det.get("confidence"),
        "capture_backend": mp.get("backend") or "pyrealsense2",
        "snapshot_tag": snap.get("tag") or "wrist_realsense",
        "updated_at": _now_iso(),
    }


def _apply_live_wrist_from_plan(mp: dict[str, Any] | None, *, cycle: str | None = None) -> None:
    live = _wrist_live_from_metric(mp, cycle=cycle)
    if live:
        _job_set(live_wrist=live, metric_plan=_sanitize_metric_plan(mp or {}))


def _run_gates(instruction: str, color_hint: str | None) -> dict[str, Any]:
    from go2_dashboard.grasp_autonomous_loop import _servo_deg7
    from go2_dashboard.orbbec_wrist_grasp import plan_wrist_grasp_metric

    servo = _servo_deg7()
    if servo is None:
        return {"ok": False, "reason": "no_servo_feedback"}
    # Gate iniziale: acquisizione depth completa (no fast) — la D456 ha bisogno di settle.
    fast_gates = _truthy("GO2_GRASP_TEACH_GATES_FAST", "0")
    mp = plan_wrist_grasp_metric(
        servo, instruction=instruction, color_hint=color_hint, fast_capture=fast_gates
    )
    if not mp.get("ok") and str(mp.get("reason") or "") == "realsense_capture_error":
        time.sleep(0.4)
        _teach_wrist_camera_prepare()
        mp = plan_wrist_grasp_metric(
            servo, instruction=instruction, color_hint=color_hint, fast_capture=fast_gates
        )
    det = mp.get("object_detection") if isinstance(mp.get("object_detection"), dict) else {}
    partial_rgb = bool(det.get("ok")) and (
        bool(mp.get("partial_rgb_ok")) or bool(mp.get("rgb_depth_fallback"))
    )
    rgb_fb = bool(mp.get("rgb_depth_fallback"))
    depth_src = mp.get("depth_source")
    gates: dict[str, Any] = {
        "depth_ok": bool(mp.get("ok") and mp.get("depth_m") is not None and not rgb_fb),
        "detect_ok": bool(det.get("ok")) if partial_rgb else bool(mp.get("ok")),
        "reach_ok": bool(mp.get("ok") and mp.get("reachable") is True),
        "absolute_ik_safe": bool(mp.get("absolute_ik_safe")),
        "partial_rgb_ok": partial_rgb,
        "rgb_depth_fallback": rgb_fb,
        "depth_m": mp.get("depth_m"),
        "depth_m_raw": mp.get("depth_m_raw"),
        "depth_source": depth_src,
        "depth_source_it": _depth_source_label_it(str(depth_src) if depth_src else None),
        "depth_support": mp.get("depth_support"),
        "reach_m": mp.get("reach_m"),
        "calib_ok": True,
        "reason": mp.get("reason"),
        "hint_it": mp.get("hint_it"),
    }
    if det.get("ok"):
        gates["confidence"] = det.get("confidence")
        gates["color_hint"] = det.get("color_hint") or color_hint
    _job_set(gates=gates)
    _apply_live_wrist_from_plan(mp, cycle="gates")
    return {"ok": bool(mp.get("ok")), "metric_plan": mp, "gates": gates}


def _sanitize_metric_plan(mp: dict[str, Any]) -> dict[str, Any]:
    """Riduce il piano metrico per polling UI (niente array enormi)."""
    if not isinstance(mp, dict):
        return {}
    det = mp.get("object_detection") if isinstance(mp.get("object_detection"), dict) else {}
    out: dict[str, Any] = {
        "ok": mp.get("ok"),
        "reason": mp.get("reason"),
        "hint_it": mp.get("hint_it"),
        "depth_m": mp.get("depth_m"),
        "depth_m_raw": mp.get("depth_m_raw"),
        "depth_source": mp.get("depth_source"),
        "depth_support": mp.get("depth_support"),
        "rgb_depth_fallback": mp.get("rgb_depth_fallback"),
        "partial_rgb_ok": mp.get("partial_rgb_ok"),
        "reachable": mp.get("reachable"),
        "reach_m": mp.get("reach_m"),
        "grasp_display_base_link_m": mp.get("grasp_display_base_link_m"),
        "target_base_link_m": mp.get("target_base_link_m"),
        "object_detection": {
            "ok": det.get("ok"),
            "confidence": det.get("confidence"),
            "color_hint": det.get("color_hint"),
            "bbox_center_px": det.get("bbox_center_px"),
            "bbox_xyxy": det.get("bbox_xyxy"),
        },
    }
    dd = mp.get("depth_diag")
    if isinstance(dd, dict):
        out["depth_diag"] = {
            "reason": dd.get("reason"),
            "support": dd.get("support"),
            "hint_it": dd.get("hint_it"),
        }
    if isinstance(mp.get("debug_snapshot"), dict):
        out["debug_snapshot"] = {
            "saved": mp["debug_snapshot"].get("saved"),
            "path": mp["debug_snapshot"].get("path"),
        }
    return out


def _run_teach_worker(
    *,
    instruction: str,
    mode: str,
    color_hint: str | None,
    max_cycles: int,
    max_picks: int,
    front_camera: int,
    use_supervisor: bool,
) -> None:
    import sys

    s = str(PROJECT_ROOT / "scripts")
    if s not in sys.path:
        sys.path.insert(0, s)
    from box_object_detector import parse_color_from_instruction

    from go2_dashboard.d1_jog import service
    from go2_dashboard.grasp_autonomous_loop import _execute_autonomous_grasp
    from go2_dashboard.grasp_collection_mission import run_collect_after_scan

    hint = color_hint or parse_color_from_instruction(instruction)
    grasp_verify: dict[str, Any] | None = None
    ok_final = False
    failed: str | None = None

    def _release_teach_orbbec() -> None:
        try:
            from go2_dashboard.d1_jog.orbbec_capture import release_orbbec_steal

            rel = release_orbbec_steal()
            if rel.get("released"):
                _append_log("info", "done", "Lock Orbbec SDK rilasciato.")
        except Exception:
            pass

    try:
        if _teach_cancelled():
            _append_log("warn", "couple", "Flusso annullato prima dell'avvio worker.")
            _job_set(
                running=False,
                ok=False,
                failed_step="couple",
                finished_at=_now_iso(),
                label_it="Flusso teach annullato.",
            )
            return

        if not _truthy("GO2_LOCAL", "0"):
            failed = "couple"
            _append_log("error", "couple", "GO2_LOCAL off — flusso teach solo su NX.")
            _set_step("couple", "fail", detail="GO2_LOCAL off")
            _job_set(
                running=False,
                ok=False,
                failed_step=failed,
                finished_at=_now_iso(),
                label_it="GO2_LOCAL non attivo.",
            )
            return

        # Pausa stream MJPEG/V4L (log.0 + log.6) per tutta la presa — evita doppio open con RealSense/SDK.
        try:
            from go2_dashboard.cameras import CAMERA_CACHE

            teach_pause_s = float(os.environ.get("GO2_TEACH_PAUSE_CAMERA_CACHE_S", "900"))
            for dev in (0, 6):
                CAMERA_CACHE.request_pause(int(dev), duration_s=teach_pause_s)
            _dbg_fdb211(
                "H6",
                "grasp_teach_flow.py:worker",
                "camera_cache_paused",
                {"devices": [0, 6], "duration_s": teach_pause_s},
            )
            _append_log(
                "info",
                "couple",
                f"Stream camera dashboard in pausa {teach_pause_s:.0f}s (presa teach).",
            )
        except Exception as exc:
            _dbg_fdb211(
                "H6",
                "grasp_teach_flow.py:worker",
                "camera_cache_pause_fail",
                {"error": repr(exc)},
            )

        # --- couple ---
        _set_step("couple", "running")
        _progress(5, "Attivo coppia braccio…")
        _append_log("info", "couple", "Verifica coppia DDS (funcode hold)…")
        couple = service.ensure_coupled_for_motion()
        if not couple.get("ok"):
            failed = "couple"
            raw_reason = str(couple.get("reason") or "coppia non attiva")
            detail = _couple_fail_hint(raw_reason)
            _dbg_fdb211("H3", "grasp_teach_flow.py:couple", "couple_failed", {"reason": raw_reason, "detail": detail[:200]})
            _set_step("couple", "fail", detail=detail[:120])
            _append_log("error", "couple", f"Coppia fallita: {detail}")
            _job_set(
                running=False,
                ok=False,
                failed_step=failed,
                finished_at=_now_iso(),
                label_it=f"Coppia braccio fallita: {detail}",
            )
            return
        _set_step("couple", "ok", detail="Coppia attiva")
        _append_log("info", "couple", "Coppia braccio attiva.")

        if _teach_cancelled():
            _set_step("couple", "fail", detail="annullato")
            _job_set(
                running=False,
                ok=False,
                failed_step="couple",
                finished_at=_now_iso(),
                label_it="Flusso teach annullato dall'operatore.",
            )
            return

        # --- scan j90 ---
        _set_step("scan_j90", "running")
        _progress(12, "Vado a Scansione +90°…")
        _append_log("info", "scan_j90", "Movimento verso waypoint Scansione +90°…")
        scan_out = _goto_scan_j90()
        if not scan_out.get("ok"):
            failed = "scan_j90"
            reason = str(scan_out.get("reason") or "errore movimento")
            _dbg_fdb211("H4", "grasp_teach_flow.py:scan_j90", "scan_failed", {"reason": reason, "scan_out": scan_out})
            if "plane_busy" in reason.lower():
                reason = (
                    f"{reason} — chiudi sessione live tab «Braccio D1 · giunti» "
                    "(Fine controllo) e riprova «Prendi»."
                )
            _set_step("scan_j90", "fail", detail=reason[:120])
            _append_log("error", "scan_j90", f"Scansione +90° fallita: {reason}")
            _job_set(
                running=False,
                ok=False,
                failed_step=failed,
                finished_at=_now_iso(),
                label_it=f"Scansione +90° fallita: {reason}",
            )
            return
        wp_name = scan_out.get("waypoint_name") or "SCANSIONE 90"
        _set_step("scan_j90", "ok", detail=str(wp_name))
        _append_log("info", "scan_j90", f"Posa Scansione +90° raggiunta ({wp_name}).")
        _progress(22)

        if _teach_cancelled():
            _set_step("scan_j90", "fail", detail="annullato")
            _job_set(
                running=False,
                ok=False,
                failed_step="scan_j90",
                finished_at=_now_iso(),
                label_it="Flusso teach annullato dall'operatore.",
            )
            return

        # --- gates ---
        _set_step("gates", "running")
        _progress(28, "Acquisizione metrica polso…")
        _append_log("info", "gates", "Preparo camera polso per acquisizione metrica…")
        steal = _teach_wrist_camera_prepare()
        if steal.get("ok"):
            if steal.get("backend") == "realsense":
                _append_log("info", "gates", "RealSense polso: pausa V4L log.0 — acquisizione metrica…")
            else:
                _append_log("info", "gates", "Orbbec SDK lock OK — acquisizione metrica polso…")
        else:
            hint_steal = steal.get("hint") or steal.get("reason") or steal.get("holder") or "camera_busy"
            _append_log(
                "warn",
                "gates",
                f"Camera polso non pronta ({hint_steal}) — provo comunque depth/detect.",
            )
        _append_log("info", "gates", "Polso: depth, detect, reach…")
        gate_out = _run_gates(instruction, hint)
        gates = gate_out.get("gates") if isinstance(gate_out.get("gates"), dict) else {}
        if gate_out.get("ok"):
            conf = gates.get("confidence")
            conf_s = ""
            if conf is not None:
                try:
                    conf_s = f" conf={float(conf):.2f}"
                except (TypeError, ValueError):
                    pass
            depth_m = gates.get("depth_m")
            depth_s = ""
            if depth_m is not None:
                try:
                    src = gates.get("depth_source_it") or gates.get("depth_source") or "?"
                    depth_s = f" depth={float(depth_m):.3f}m ({src})"
                    if gates.get("depth_support") is not None:
                        depth_s += f" support={gates.get('depth_support')}"
                except (TypeError, ValueError):
                    pass
            if gates.get("reach_ok"):
                _set_step("gates", "ok", detail=f"detect+reach OK{conf_s}{depth_s}")
                _append_log("info", "gates", "Gate metrici OK — oggetto rilevato e raggiungibile.")
            else:
                _set_step("gates", "ok", detail=f"detect OK, reach NO{conf_s}{depth_s}")
                mp_full = gate_out.get("metric_plan") if isinstance(gate_out.get("metric_plan"), dict) else {}
                if mp_full.get("rgb_depth_fallback"):
                    _append_log(
                        "warn",
                        "gates",
                        f"Depth D456 assente — uso stima RGB da bbox{depth_s}; avvicinamento verso oggetto.",
                    )
                else:
                    _append_log(
                        "warn",
                        "gates",
                        f"Oggetto rilevato ma fuori reach{depth_s} — avvicinamento visivo (servo polso).",
                    )
        else:
            reason = str(gates.get("reason") or gate_out.get("reason") or "no_detection")
            hint_it = gates.get("hint_it") or reason
            _set_step("gates", "fail", detail=str(hint_it)[:120])
            _append_log("warn", "gates", f"Gate: {hint_it} — continuo con loop autonomo.")
        _progress(35)

        # --- grasp ---
        _set_step("grasp", "running")
        mode_label = "raccolta" if mode == "collect" else "presa singola"
        _progress(40, f"Avvio {mode_label}…")
        _append_log("info", "grasp", f"Modalità {mode_label} — istruzione: {instruction[:80]}")

        def _on_grasp_progress(**kw: Any) -> None:
            if "label_it" in kw:
                _job_set(label_it=kw["label_it"])
                _append_log("info", "grasp", str(kw["label_it"]))
            if "cycle" in kw and isinstance(kw["cycle"], dict):
                cyc = kw["cycle"]
                step_name = cyc.get("step", "?")
                mp_cycle: dict[str, Any] = {
                    "ok": cyc.get("metric_ok"),
                    "reason": cyc.get("reason"),
                    "depth_m": cyc.get("depth_m"),
                    "depth_m_raw": cyc.get("depth_m_raw"),
                    "depth_source": cyc.get("depth_source"),
                    "depth_support": cyc.get("depth_support"),
                    "depth_diag": cyc.get("depth_diag"),
                    "rgb_depth_fallback": cyc.get("rgb_depth_fallback"),
                    "partial_rgb_ok": cyc.get("reason") in {"rgb_approach_no_depth", "rgb_depth_estimate_only"},
                    "reachable": cyc.get("reachable"),
                    "reach_m": cyc.get("reach_m"),
                    "object_detection": cyc.get("object_detection"),
                }
                _apply_live_wrist_from_plan(mp_cycle, cycle=step_name)
                dm = cyc.get("depth_m")
                src_it = cyc.get("depth_source_it") or _depth_source_label_it(cyc.get("depth_source"))
                if dm is not None:
                    depth_line = f"depth={float(dm):.3f}m ({src_it})"
                    if cyc.get("depth_support") is not None:
                        depth_line += f" · support={cyc.get('depth_support')}"
                    if cyc.get("depth_diag_reason"):
                        depth_line += f" · diag={cyc.get('depth_diag_reason')}"
                    _job_set(label_it=f"{step_name} — {depth_line}")
                if cyc.get("rgb_approach_applied"):
                    _append_log(
                        "info",
                        "grasp",
                        f"{step_name}: avvicinamento RGB verso bbox"
                        + (" · depth stimata" if cyc.get("rgb_depth_fallback") else ""),
                    )
                elif cyc.get("reason") == "no_coach_target":
                    dm_s = f" depth={dm}m" if dm is not None else ""
                    _append_log(
                        "warn",
                        "grasp",
                        f"{step_name}: nessun waypoint IK{dm_s} — servo visivo, riprovo…",
                    )
                elif cyc.get("reason") == "rgb_approach_no_depth":
                    dm_s = f" depth={dm:.3f}m ({src_it})" if dm is not None else ""
                    _append_log("warn", "grasp", f"{step_name}: D456 senza depth nel bbox{dm_s} — solo servo RGB")
                elif cyc.get("metric_ok") is False:
                    _append_log("warn", "grasp", f"{step_name}: {cyc.get('reason', 'metric fail')}")
                elif cyc.get("ok"):
                    _append_log("info", "grasp", f"{step_name}: OK")
                else:
                    mot = cyc.get("motion") if isinstance(cyc.get("motion"), dict) else {}
                    psteps = mot.get("partial_steps") if isinstance(mot.get("partial_steps"), list) else []
                    fail_reason = mot.get("reason")
                    if not fail_reason and psteps:
                        fail_reason = (psteps[0] or {}).get("reason")
                    _append_log(
                        "info",
                        "grasp",
                        f"{step_name}: stage={cyc.get('coach_stage')} "
                        f"motion_ok={mot.get('ok')} reason={fail_reason or '—'} "
                        f"dist_grasp_m={cyc.get('dist_to_grasp_m')} "
                        f"tcp_ok={mot.get('tcp_reach_ok')} blend={mot.get('approach_blend_applied')}",
                    )
            base = 40
            span = 45
            if mode == "single":
                _progress(base + span * 0.5)
            else:
                picks = teach_flow_status().get("picks_done") or 0
                mp = max(1, max_picks)
                _progress(base + span * min(1.0, picks / mp))

        if mode == "collect":
            targets: list[str] = []
            parsed = parse_color_from_instruction(instruction)
            if parsed:
                targets = [parsed]
            else:
                targets = ["blu"]

            def _on_collect_log(level: str, step: str, msg: str) -> None:
                _append_log(level, step, msg)

            def _on_collect_step(step: dict[str, Any]) -> None:
                if step.get("step", "").startswith("grasp_") and step.get("ok"):
                    with _JOB_LOCK:
                        _JOB["picks_done"] = int(_JOB.get("picks_done") or 0) + 1

            collect_result = run_collect_after_scan(
                targets=targets,
                max_picks=max_picks,
                instruction=instruction,
                front_camera=front_camera,
                skip_scan=True,
                on_progress=_on_grasp_progress,
                on_log=_on_collect_log,
                on_step=_on_collect_step,
            )
            ok_final = bool(collect_result.get("ok"))
            picks = int(collect_result.get("picks_done") or 0)
            _job_set(picks_done=picks)
            if ok_final:
                _set_step("grasp", "ok", detail=f"{picks} prese completate")
                _append_log("info", "grasp", f"Raccolta: {picks} prese.")
            else:
                failed = "grasp"
                _set_step("grasp", "fail", detail=str(collect_result.get("label_it") or "raccolta fallita"))
                _append_log("error", "grasp", str(collect_result.get("label_it") or "Raccolta fallita."))
            grasp_verify = collect_result.get("last_grasp_verify")
        else:
            seed_mp = gate_out.get("metric_plan") if isinstance(gate_out.get("metric_plan"), dict) else None
            result = _execute_autonomous_grasp(
                instruction=instruction or "prendi la scatola",
                color_hint=hint,
                max_cycles=max_cycles,
                use_supervisor=use_supervisor,
                on_progress=_on_grasp_progress,
                seed_metric_plan=seed_mp,
            )
            ok_final = bool(result.get("grasp_detected"))
            grasp_verify = result.get("grasp_verify") if isinstance(result.get("grasp_verify"), dict) else None
            cycles = result.get("cycles") if isinstance(result.get("cycles"), list) else []
            if cycles:
                _job_set(autonomous_cycles=cycles[-8:])
            if ok_final:
                _set_step("grasp", "ok", detail="Presa autonoma completata")
                _append_log("info", "grasp", "Loop presa autonomo OK.")
            else:
                failed = "grasp"
                lbl = str(result.get("label_it") or "presa fallita")
                _set_step("grasp", "fail", detail=lbl[:120])
                _append_log("error", "grasp", lbl)

        _progress(88)
        _job_set(grasp_verify=grasp_verify)

        # --- verify ---
        _set_step("verify", "running")
        _progress(92, "Verifica pinza…")
        if isinstance(grasp_verify, dict) and grasp_verify.get("ok"):
            _set_step("verify", "ok", detail="Oggetto in pinza")
            _append_log("info", "verify", "Verifica pinza positiva.")
        elif ok_final:
            _set_step("verify", "ok", detail="Presa completata (verify non disponibile)")
            _append_log("info", "verify", "Presa OK — verify non restituito.")
        else:
            failed = failed or "verify"
            reason = (
                str(grasp_verify.get("reason") or grasp_verify.get("hint_it") or "pinza vuota")
                if isinstance(grasp_verify, dict)
                else "presa non riuscita"
            )
            _set_step("verify", "fail", detail=reason[:120])
            _append_log("error", "verify", reason)

        # --- done ---
        if ok_final:
            _set_step("done", "ok", detail="Flusso completato")
            _append_log("info", "done", "Flusso teach completato con successo.")
            _dbg_fdb211("H5", "grasp_teach_flow.py:done", "flow_ok", {"picks_done": _JOB.get("picks_done")})
            _job_set(
                running=False,
                ok=True,
                failed_step=None,
                finished_at=_now_iso(),
                progress_pct=100,
                label_it="Flusso completato — presa OK.",
            )
        else:
            _set_step("done", "fail", detail=f"Interrotto su {failed or 'grasp'}")
            _append_log("error", "done", f"Flusso terminato con errore (step {failed or 'grasp'}).")
            _dbg_fdb211(
                "H5",
                "grasp_teach_flow.py:done",
                "flow_fail",
                {"failed_step": failed or "grasp", "grasp_verify": grasp_verify},
            )
            _job_set(
                running=False,
                ok=False,
                failed_step=failed or "grasp",
                finished_at=_now_iso(),
                progress_pct=100,
                label_it=f"Flusso interrotto su «{failed or 'grasp'}».",
            )

        try:
            log_path = PROJECT_ROOT / "data" / "grasp_teach_flow_last.json"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                json.dumps(teach_flow_status(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
    except Exception as exc:
        step = failed or "grasp"
        _append_log("error", step, f"Errore: {exc!r}")
        _job_set(
            running=False,
            ok=False,
            failed_step=step,
            finished_at=_now_iso(),
            label_it=f"Errore flusso teach: {exc!r}",
        )
    finally:
        _release_teach_orbbec()


def start_teach_flow(
    *,
    instruction: str = "",
    confirm: str | None = None,
    color_hint: str | None = None,
    max_cycles: int | None = None,
    max_picks: int | None = None,
    front_camera: int = 6,
    use_supervisor: bool | None = None,
) -> tuple[dict[str, Any], int]:
    import json
    import time

    # #region agent log
    _t0 = time.perf_counter()

    def _dbg_flow(hypothesis_id: str, message: str, data: dict) -> None:
        try:
            p = PROJECT_ROOT / "data" / "debug-16a61f.ndjson"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "sessionId": "16a61f",
                            "hypothesisId": hypothesis_id,
                            "location": "grasp_teach_flow.py:start_teach_flow",
                            "message": message,
                            "data": data,
                            "timestamp": int(time.time() * 1000),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass

    _dbg_flow("H2", "start_teach_flow_enter", {"confirm": bool(confirm == CONFIRM_TOKEN)})
    # #endregion

    with _JOB_LOCK:
        if _JOB.get("running"):
            snap = json.loads(json.dumps({k: v for k, v in _JOB.items()}, default=str))
            return (
                {"ok": False, "reason": "job_already_running", "status": snap},
                409,
            )

    if _other_jobs_running():
        # #region agent log
        _dbg_flow("H4", "start_teach_flow_other_job", {"ms": round((time.perf_counter() - _t0) * 1000.0, 2)})
        # #endregion
        return (
            {"ok": False, "reason": "other_job_running", "status": teach_flow_status()},
            409,
        )

    instr = (instruction or "prendi la scatola").strip()
    mode = "collect" if _is_collect_mode(instr) else "single"
    mc = max_cycles if max_cycles is not None else int(_envf("GO2_GRASP_AUTONOMOUS_MAX_CYCLES", 24))
    mc = max(1, min(mc, 60))
    mp = max_picks if max_picks is not None else int(_envf("GO2_COLLECT_MAX_PICKS", 3))
    mp = max(1, min(mp, 12))
    sup = use_supervisor if use_supervisor is not None else (
        False
        if _truthy("GO2_GRASP_TEACH_FAST", "1")
        else _truthy("GO2_GRASP_COACH_SUPERVISOR", "1")
    )

    if confirm != CONFIRM_TOKEN:
        return (
            {
                "ok": True,
                "started": False,
                "dry_run": True,
                "confirm_required": CONFIRM_TOKEN,
                "mode": mode,
                "instruction": instr,
                "max_cycles": mc,
                "max_picks": mp,
                "use_supervisor": sup,
                "steps_preview": _fresh_steps(),
                "hint_it": f"Dry-run — ripeti con confirm={CONFIRM_TOKEN!r} per eseguire.",
            },
            200,
        )

    _CANCEL.clear()
    _job_set(
        running=True,
        ok=None,
        mode=mode,
        started_at=_now_iso(),
        finished_at=None,
        failed_step=None,
        current_step="couple",
        label_it="Avvio flusso teach…",
        progress_pct=0,
        steps=_fresh_steps(),
        log_lines=[],
        params={
            "instruction": instr,
            "mode": mode,
            "color_hint": color_hint,
            "max_cycles": mc,
            "max_picks": mp,
            "front_camera": front_camera,
        },
        gates=None,
        metric_plan=None,
        grasp_verify=None,
        picks_done=0,
    )
    _append_log("info", "couple", f"Flusso avviato — modalità {mode}.")

    th = threading.Thread(
        target=_run_teach_worker,
        kwargs={
            "instruction": instr,
            "mode": mode,
            "color_hint": color_hint,
            "max_cycles": mc,
            "max_picks": mp,
            "front_camera": front_camera,
            "use_supervisor": sup,
        },
        name="grasp_teach_flow",
        daemon=True,
    )
    th.start()
    # #region agent log
    _dbg_flow(
        "H2",
        "start_teach_flow_thread_started",
        {"ms": round((time.perf_counter() - _t0) * 1000.0, 2), "mode": mode},
    )
    # #endregion
    return (
        {
            "ok": True,
            "started": True,
            "mode": mode,
            "poll": "/api/grasp/teach_status",
            "status": teach_flow_status(),
        },
        202,
    )


def cancel_teach_flow(*, reason_it: str | None = None) -> dict[str, Any]:
    """Annulla job teach in corso (libera UI e permette un nuovo «Prendi»)."""
    _CANCEL.set()
    with _JOB_LOCK:
        running = bool(_JOB.get("running"))
        step = str(_JOB.get("current_step") or "couple")
    if running:
        _append_log("warn", step, reason_it or "Annullamento richiesto dall'operatore.")
        _job_set(
            running=False,
            ok=False,
            failed_step=step,
            finished_at=_now_iso(),
            label_it=reason_it or "Flusso teach annullato dall'operatore.",
        )
    return {
        "ok": True,
        "cancelled": running,
        "was_running": running,
        "status": teach_flow_status(),
    }

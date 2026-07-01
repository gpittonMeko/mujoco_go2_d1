"""Movimento braccio — **stesso percorso del tab Operatori «Braccio D1 · giunti»**.

Tab UI (funzionante):
  1. ``POST /api/arm/joints/couple`` (opz.)
  2. ``POST /api/arm/joints/session_begin`` → ``d1_arm_motion.begin_live_session`` → ``joint_control_begin``
  3. ``POST /api/arm/joints/goto_deg`` (Smooth) → ``publish_goto_servo_deg7``
  4. Slider live → ``publish_live_pose_deg7`` → ``jog_pose_deg`` sul daemon

Tutta la presa / fold / START deve usare **solo** questo modulo, mai burst one-shot o ``arm_motion_session_begin`` con power separato.
"""

from __future__ import annotations

import os
from typing import Any

from go2_dashboard import d1_arm_motion
from go2_dashboard.d1_arm_publish_lite import editor_max_step_deg_7, read_servo_deg_with_diag
from go2_dashboard.paths import PROJECT_ROOT


def _editor_goto_delay_ms(delay_ms: int | None) -> int:
    if delay_ms is not None:
        return max(70, int(delay_ms))
    try:
        return max(70, int((os.environ.get("D1_EDITOR_MOVE_DELAY_MS") or "340").strip()))
    except ValueError:
        return 340


def _reconcile_dds_if_enabled() -> dict[str, Any] | None:
    # DEFAULT OFF: uccidere+riavviare il daemon all'avvio sessione crea un gap senza publisher
    # DDS → il firmware D1 perde la liveliness e il braccio CADE. Il tab «Braccio D1 · giunti»
    # (che funziona) non fa mai questo kill. Vedi .cursor/rules/d1-arm-funcode-hold.mdc.
    if os.environ.get("GO2_GRASP_RECONCILE_DDS", "0").lower() not in {"1", "true", "yes", "on"}:
        return None
    return {
        "ok": False,
        "skipped": True,
        "reason": "dds_reconcile_removed_external_hold_owner",
        "safety_interlock": True,
    }


def _operator_live_tick_after_session() -> dict[str, Any]:
    """Stesso tick iniziale del tab Giunti: ``session_begin`` poi subito ``live_deg``."""
    from go2_dashboard.d1_jog import service as jog_svc

    fb = jog_svc.read_servo_deg(fast=True)
    if not fb.get("ok") or not fb.get("servo_deg"):
        return {"ok": False, "reason": "no_feedback", "action": "operator_live_tick"}
    sd = fb["servo_deg"]
    tick = jog_svc.jog_pose_deg(sd, keep_lock=True)
    tick["action"] = "operator_live_tick"
    return tick


def begin_operator_arm_session(*, servo_deg: list[float] | None = None) -> dict[str, Any]:
    """Identico a «Controllo live ON» + retry couple come ``operators_arm_joints.js``."""
    if d1_arm_motion.is_live_session_active():
        tick = _operator_live_tick_after_session()
        return {
            "ok": True,
            "skipped": True,
            "action": "operator_arm_session",
            "live_session": True,
            "live_tick": tick,
        }
    from go2_dashboard.d1_jog import service as jog_svc

    recon = _reconcile_dds_if_enabled()
    couple = jog_svc.ensure_coupled_for_motion()
    if not (couple.get("ok") or couple.get("skipped")):
        return {**couple, "action": "operator_arm_session"}

    out = d1_arm_motion.begin_live_session(servo_deg=servo_deg)
    if not out.get("ok") and out.get("reason") == "not_coupled":
        jog_svc.ensure_coupled_for_motion()
        out = d1_arm_motion.begin_live_session(servo_deg=servo_deg)
    out["action"] = "operator_arm_session"
    out["live_session"] = bool(d1_arm_motion.is_live_session_active())
    if recon is not None:
        out["dds_reconcile"] = recon
    if out.get("ok") and out.get("live_session"):
        out["live_tick"] = _operator_live_tick_after_session()
    return out


def hold_operator_arm_pose() -> dict[str, Any]:
    """Mantiene la posa corrente sul daemon (funcode 2 stream), come hold del jog."""
    from go2_dashboard.d1_jog import service as jog_svc

    if not d1_arm_motion.is_live_session_active():
        beg = begin_operator_arm_session()
        if not (beg.get("ok") or beg.get("skipped")):
            return beg
    fb = jog_svc.read_servo_deg(fast=True)
    if not fb.get("ok") or not fb.get("servo_deg"):
        return {"ok": False, "reason": "no_feedback", "action": "hold_operator_arm_pose"}
    return jog_svc.hold_pose_stream(servo_deg=fb["servo_deg"])


def goto_servo_deg7_operator(
    target7_deg: list[float],
    *,
    max_step_deg: list[float] | None = None,
    delay_ms: int | None = None,
) -> dict[str, Any]:
    """Identico a «Smooth (goto)» nel tab giunti — sessione live + ``publish_goto_servo_deg7``."""
    from go2_dashboard.d1_arm_publish_lite import publish_goto_servo_deg7

    beg = begin_operator_arm_session()
    if not (beg.get("ok") or beg.get("skipped")):
        return beg

    steps = max_step_deg if max_step_deg is not None else editor_max_step_deg_7()
    out = publish_goto_servo_deg7(
        target7_deg,
        max_step_deg=steps,
        delay_ms=_editor_goto_delay_ms(delay_ms),
        skip_prehold=True,
    )
    out["motion_path"] = "operator_ui_goto_deg"
    hold_operator_arm_pose()
    return out


def goto_servo_deg7_operator_staged(
    target7_deg: list[float],
    *,
    max_step_deg: list[float] | None = None,
    delay_ms: int | None = None,
    partial_fractions: list[float] | None = None,
) -> dict[str, Any]:
    """Progressioni parziali in spazio giunti (waypoint = blend verso target)."""
    angles_fb, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    if angles_fb is None or len(angles_fb) < 7:
        return {"ok": False, "reason": "no_servo_feedback", "diag": diag}
    cur = [round(float(angles_fb[i]), 3) for i in range(7)]
    tgt = [round(float(target7_deg[i]), 3) for i in range(7)]
    raw = (os.environ.get("GO2_GRASP_PARTIAL_FRACTIONS") or "0.10,0.22,0.36,0.52,0.70,0.88,1.0").strip()
    try:
        fracs = partial_fractions or [float(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        fracs = [0.10, 0.22, 0.36, 0.52, 0.70, 0.88, 1.0]
    fracs = sorted({max(0.05, min(1.0, f)) for f in fracs})
    partial_steps: list[dict[str, Any]] = []
    ok_all = True
    for frac in fracs:
        wp = [round(cur[i] + float(frac) * (tgt[i] - cur[i]), 3) for i in range(7)]
        r = goto_servo_deg7_operator(wp, max_step_deg=max_step_deg, delay_ms=delay_ms)
        r["partial_fraction"] = float(frac)
        partial_steps.append(r)
        if not r.get("ok"):
            ok_all = False
            break
    return {
        "ok": ok_all,
        "mode": "operator_ui_goto_staged",
        "partial_count": len(partial_steps),
        "partial_steps": partial_steps,
        "target_servo_deg_7": tgt,
    }

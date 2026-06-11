"""Verifica post-presa della pinza D1 basata sullo stallo di posizione del giunto.

L'hardware D1 espone solo gli angoli servo (nessun feedback di corrente/coppia), quindi la
verifica e' un'euristica di posizione: dopo aver comandato la chiusura, se la pinza NON ha
raggiunto l'angolo di chiusura piena si e' fermata su un oggetto (presa rilevata); se ha
raggiunto l'angolo comandato si e' chiusa a vuoto.

Condivisa da:
- coach (``grasp_coach_agent.grasp_coach_step``)
- guided a fasi (``grasp_phased_execute.execute_phased_from_cached_plan``)
"""

from __future__ import annotations

import os
import time
from typing import Any

GRIPPER_JOINT_INDEX = 6


def _stall_margin_deg() -> float:
    raw = (os.environ.get("GO2_GRASP_GRIPPER_STALL_MARGIN_DEG") or "").strip()
    if not raw:
        return 3.0
    try:
        return float(raw)
    except ValueError:
        return 3.0


def verify_gripper_grasp(close_deg_commanded: float, *, hold_s: float = 0.6) -> dict[str, Any]:
    """Verifica se la pinza ha afferrato un oggetto dopo un comando di chiusura.

    Attende ``hold_s`` perche' il giunto si assesti, poi legge l'angolo del giunto pinza
    (indice 6) e lo confronta con l'angolo comandato. Non solleva mai eccezioni e non blocca
    il flusso: in caso di assenza di feedback ritorna ``{"ok": False, ...}``.
    """
    out: dict[str, Any] = {
        "ok": False,
        "method": "position_stall",
        "gripper_deg_commanded": round(float(close_deg_commanded), 2),
        "stall_margin_deg": round(_stall_margin_deg(), 2),
    }
    try:
        wait_s = max(0.0, float(hold_s))
    except (TypeError, ValueError):
        wait_s = 0.6
    if wait_s > 0:
        time.sleep(wait_s)

    try:
        from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag
        from go2_dashboard.paths import PROJECT_ROOT

        servo, diag = read_servo_deg_with_diag(PROJECT_ROOT)
    except Exception as exc:  # pragma: no cover - difensivo
        out["reason"] = "servo_read_error"
        out["detail"] = repr(exc)
        return out

    if not isinstance(servo, (list, tuple)) or len(servo) <= GRIPPER_JOINT_INDEX:
        out["reason"] = "no_servo_feedback"
        if isinstance(diag, dict):
            out["servo_backend"] = diag.get("backend")
        return out

    try:
        achieved = float(servo[GRIPPER_JOINT_INDEX])
    except (TypeError, ValueError):
        out["reason"] = "bad_gripper_angle"
        return out

    margin = _stall_margin_deg()
    # Chiusura: angoli piu' negativi = pinza piu' chiusa. Se l'angolo raggiunto resta sopra
    # l'angolo comandato di chiusura piena (oltre il margine), la pinza ha stallato su un oggetto.
    stall_deg = achieved - float(close_deg_commanded)
    grasp_detected = stall_deg > margin

    out.update(
        {
            "ok": True,
            "gripper_deg_achieved": round(achieved, 2),
            "stall_deg": round(stall_deg, 2),
            "grasp_detected": bool(grasp_detected),
        }
    )
    if isinstance(diag, dict):
        out["servo_backend"] = diag.get("backend")
    return out

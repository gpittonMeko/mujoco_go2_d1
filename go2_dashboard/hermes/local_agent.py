"""Risposte Hermes senza cloud: contesto operator + domanda utente."""

from __future__ import annotations

import re
from typing import Any


def _cam_summary(cameras: dict[str, Any]) -> str:
    cams = cameras.get("cameras") or {}
    if not isinstance(cams, dict):
        return "Stato camere non disponibile."
    parts: list[str] = []
    for key in ("0", "6"):
        st = cams.get(key) or cams.get(int(key)) if isinstance(cams, dict) else None
        if not isinstance(st, dict):
            continue
        label = "polso Orbbec" if key == "0" else "RealSense frontale"
        if st.get("error"):
            parts.append(f"{label}: errore {st.get('error')}")
        elif st.get("available") or st.get("jpg"):
            parts.append(f"{label}: attiva")
        else:
            parts.append(f"{label}: non disponibile")
    return ". ".join(parts) if parts else "Nessun dato camera nel contesto."


def _arm_summary(scene: dict[str, Any]) -> str:
    deg = scene.get("servo_deg")
    if isinstance(deg, list) and len(deg) >= 6:
        vals = ", ".join(f"J{i+1}={d}°" for i, d in enumerate(deg[:6]))
        return f"Braccio: {vals}."
    return "Braccio: feedback giunti non presente."


def _grasp_summary(grasp: dict[str, Any]) -> str:
    steps = grasp.get("steps")
    if isinstance(steps, list) and steps:
        return f"Pipeline presa: {len(steps)} passi. " + (steps[0] if isinstance(steps[0], str) else "")
    hint = grasp.get("hint_it") or grasp.get("note")
    if hint:
        return str(hint)[:300]
    return "Pipeline presa: nessun dettaglio nel contesto."


def local_reply(user_message: str, ctx: dict[str, Any]) -> str:
    q = (user_message or "").strip().lower()
    cameras = ctx.get("cameras") or {}
    grasp = ctx.get("grasp_pipeline") or {}
    scene = ctx.get("scene_3d") or {}
    op_ok = bool(ctx.get("operator_reachable"))

    if re.search(r"\b(camer|video|orbbec|realsense|rgb)\b", q):
        if op_ok:
            return _cam_summary(cameras)
        return (
            "Hermes usa la camera Orbbec del polso (dashboard D1 :5053) e la frontale RealSense "
            "senza la operator :5052. Chiedimi «cosa vedi» o «guarda davanti»."
        )
    if re.search(r"\b(braccio|giunt|servo|joint|tcp)\b", q):
        if op_ok:
            return _arm_summary(scene)
        return "Per lo stato giunti usa la dashboard jog D1 sulla porta 5053."
    if re.search(r"\b(presa|grasp|afferra|box|oggetto)\b", q):
        if op_ok:
            return _grasp_summary(grasp)
        return "La pipeline presa è sulla dashboard D1 jog (:5053), non serve la operator :5052."
    if re.search(r"\b(crouch|accucci|stand|rialz|alzat)\b", q):
        return (
            "Chiedimi «alzati» o «accucciati» e comando il Go2 via Sport SDK (senza :5052). "
            "Per muovermi: «fai due passi avanti», «gira a sinistra», «fermati»."
        )
    if re.search(r"\b(passo|passi|avanti|indietro|fermat|gir[a-z]*|muov)\b", q):
        return (
            "Posso muovermi con comandi tipo: «due passi avanti», «un passo indietro», "
            "«gira a destra», «fermati», «saluta», «stretch»."
        )
    if re.search(r"\b(ciao|salve|hello|chi sei|hermes)\b", q):
        return (
            "Sono Hermes, operatore vocale sul Go2. "
            "Posso alzarti o accucciarti, muovermi («due passi avanti»), descrivere cosa vedo dalle camere, "
            "e rispondere senza la dashboard operator sulla 5052."
        )

    if op_ok:
        return (
            f"{_cam_summary(cameras)} "
            f"{_arm_summary(scene)} "
            f"{_grasp_summary(grasp)}"
        )[:500]

    return (
        "Hermes attivo sulla 5054 — indipendente dalla operator 5052. "
        "Prova: «cosa vedi», «alzati», «accucciati», «due passi avanti», «fermati»."
    )

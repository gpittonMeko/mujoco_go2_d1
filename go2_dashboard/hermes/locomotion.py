"""Intent locomozione Go2 da linguaggio naturale (italiano) → Sport SDK Move."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LocomotionIntent:
    kind: str  # move | stop | hello | stretch | sit | recovery
    vx: float = 0.0
    vy: float = 0.0
    vyaw: float = 0.0
    duration_s: float = 0.0
    steps: int | None = None
    label_it: str = ""


_NUM_WORDS: dict[str, int] = {
    "un": 1,
    "uno": 1,
    "una": 1,
    "due": 2,
    "tre": 3,
    "quattro": 4,
    "cinque": 5,
    "sei": 6,
    "sette": 7,
    "otto": 8,
    "nove": 9,
    "dieci": 10,
}

_STOP_RE = re.compile(
    r"\b("
    r"ferm(?:ati|a|o)|stop|blocca(?:ti|)?|"
    r"non\s+muover(?:ti|e)|stai\s+ferm[oa]|"
    r"stop\s+move|ferma(?:ti|)?"
    r")\b",
    re.I,
)
_HELLO_RE = re.compile(r"\b(saluta|hello|ciao\s+robot|fai\s+ciao)\b", re.I)
_STRETCH_RE = re.compile(r"\b(stretch|allung(?:ati|a)|stretching)\b", re.I)
_SIT_RE = re.compile(r"\b(siediti|sit\b|sied(?:iti|etevi))\b", re.I)
_RECOVERY_RE = re.compile(r"\b(recovery|recupera|rialzati\s+subito)\b", re.I)

_TURN_RE = re.compile(r"\b(gir[a-z]*|ruot[a-z]*|rotate|turn)\b", re.I)
_MOVE_RE = re.compile(
    r"\b("
    r"pass(?:o|i)|cammin(?:a|are)|muov(?:iti|ere|imento)|"
    r"vai|avanz(?:a|are)|indietro|indietro|"
    r"sinistra|destra|gir(?:a|are)|ruot(?:a|are)|"
    r"strafe|laterale|avanti|backward|forward|rotate|turn"
    r")\b",
    re.I,
)


def _env_float(key: str, default: float) -> float:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_step_count(q: str) -> int | None:
    m = re.search(r"\b(\d{1,2})\s*(?:pass(?:o|i)|step)\b", q, re.I)
    if m:
        return max(1, min(20, int(m.group(1))))
    for word, n in _NUM_WORDS.items():
        if re.search(rf"\b{word}\s+pass(?:o|i)\b", q, re.I):
            return n
        if re.search(rf"\b{word}\b", q, re.I) and _MOVE_RE.search(q):
            return n
    if re.search(r"\b(un\s+)?pass(?:o|i)\b", q, re.I):
        return 1
    if re.search(r"\b(piccol[oa]\s+)?movimento\b", q, re.I):
        return 1
    return None


def _default_steps(q: str) -> int:
    n = _parse_step_count(q)
    if n is not None:
        return n
    if re.search(r"\b(poco|breve|legger[oa])\b", q, re.I):
        return 1
    if re.search(r"\b(tant[oa]|molto|lungo|lontano)\b", q, re.I):
        return 4
    return max(1, int(_env_float("HERMES_MOVE_DEFAULT_STEPS", 2)))


def parse_locomotion_intent(message: str) -> LocomotionIntent | None:
    q = (message or "").strip().lower()
    if not q:
        return None

    if _STOP_RE.search(q) and not re.search(r"\b(passo|passi|avanti|indietro)\b", q, re.I):
        return LocomotionIntent(kind="stop", label_it="fermo")

    if _HELLO_RE.search(q):
        return LocomotionIntent(kind="hello", label_it="saluto")

    if _STRETCH_RE.search(q):
        return LocomotionIntent(kind="stretch", label_it="stretch")

    if _SIT_RE.search(q):
        return LocomotionIntent(kind="sit", label_it="seduto")

    if _RECOVERY_RE.search(q):
        return LocomotionIntent(kind="recovery", label_it="recovery")

    if not _MOVE_RE.search(q):
        return None

    steps = _default_steps(q)
    step_dur = _env_float("HERMES_STEP_DURATION_S", 0.45)
    vx_d = _env_float("HERMES_MOVE_VX", 0.25)
    vy_d = _env_float("HERMES_MOVE_VY", 0.22)
    vyaw_d = _env_float("HERMES_MOVE_VYAW", 0.45)

    vx = vy = vyaw = 0.0
    label = f"{steps} passi"

    if re.search(r"\b(indietro|backward|back)\b", q, re.I):
        vx = -abs(vx_d)
        label = f"{steps} passi indietro"
    elif re.search(r"\b(avanti|forward|davanti|avanz)\b", q, re.I) or re.search(r"\bpass", q, re.I):
        vx = abs(vx_d)
        label = f"{steps} passi avanti"
    elif re.search(r"\b(sinistr[ae]|left)\b", q, re.I):
        if _TURN_RE.search(q):
            vyaw = abs(vyaw_d)
            label = "gira a sinistra"
            steps = max(1, steps // 2) if steps > 1 else 1
            step_dur = _env_float("HERMES_TURN_STEP_DURATION_S", 0.55)
        else:
            vy = abs(vy_d)
            label = f"{steps} passi a sinistra"
    elif re.search(r"\b(destr[ae]|right)\b", q, re.I):
        if _TURN_RE.search(q):
            vyaw = -abs(vyaw_d)
            label = "gira a destra"
            steps = max(1, steps // 2) if steps > 1 else 1
            step_dur = _env_float("HERMES_TURN_STEP_DURATION_S", 0.55)
        else:
            vy = -abs(vy_d)
            label = f"{steps} passi a destra"
    elif _TURN_RE.search(q):
        vyaw = abs(vyaw_d)
        label = "gira"
        steps = 1
        step_dur = _env_float("HERMES_TURN_STEP_DURATION_S", 0.55)
    else:
        vx = abs(vx_d)
        label = f"{steps} passi avanti"

    duration_s = max(0.1, steps * step_dur)
    return LocomotionIntent(
        kind="move",
        vx=vx,
        vy=vy,
        vyaw=vyaw,
        duration_s=duration_s,
        steps=steps,
        label_it=label,
    )


def locomotion_to_reply(intent: LocomotionIntent, *, ok: bool) -> str:
    if not ok:
        return f"Non sono riuscito: {intent.label_it or intent.kind}."
    if intent.kind == "move":
        return f"Ok, {intent.label_it}."
    labels = {
        "stop": "Fermo.",
        "hello": "Ciao!",
        "stretch": "Stretch fatto.",
        "sit": "Seduto.",
        "recovery": "Recovery eseguito.",
    }
    return labels.get(intent.kind, "Fatto.")


def matches_locomotion_intent(message: str) -> bool:
    return parse_locomotion_intent(message) is not None

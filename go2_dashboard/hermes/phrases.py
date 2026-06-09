"""Frasi brevi fisse per interazione naturale (ack → azione → completamento)."""

from __future__ import annotations

from typing import TypedDict


class PhraseSet(TypedDict):
    ack: str
    done: str
    fail: str
    ack_key: str
    done_key: str
    fail_key: str


PHRASES: dict[str, PhraseSet] = {
    "stand_up": {
        "ack_key": "stand_ack",
        "done_key": "stand_done",
        "fail_key": "stand_fail",
        "ack": "Ti alzo.",
        "done": "Fatto. Sono in piedi.",
        "fail": "Non sono riuscito ad alzarmi.",
    },
    "crouch": {
        "ack_key": "crouch_ack",
        "done_key": "crouch_done",
        "fail_key": "crouch_fail",
        "ack": "Mi accuccio.",
        "done": "Fatto. Sono accucciato.",
        "fail": "Non sono riuscito ad accucciarmi.",
    },
    "vision": {
        "ack_key": "vision_ack",
        "done_key": "vision_done",
        "fail_key": "vision_fail",
        "ack": "Guardo.",
        "done": "Ecco cosa vedo.",
        "fail": "Non riesco a vedere.",
    },
    "move": {
        "ack_key": "move_ack",
        "done_key": "move_done",
        "fail_key": "move_fail",
        "ack": "Ok, mi muovo.",
        "done": "Fatto.",
        "fail": "Non sono riuscito a muovermi.",
    },
    "stop": {
        "ack_key": "stop_ack",
        "done_key": "stop_done",
        "fail_key": "stop_fail",
        "ack": "Mi fermo.",
        "done": "Fermo.",
        "fail": "Non sono riuscito a fermarmi.",
    },
    "hello": {
        "ack_key": "hello_ack",
        "done_key": "hello_done",
        "fail_key": "hello_fail",
        "ack": "Ciao!",
        "done": "Saluto fatto.",
        "fail": "Non sono riuscito a salutare.",
    },
    "stretch": {
        "ack_key": "stretch_ack",
        "done_key": "stretch_done",
        "fail_key": "stretch_fail",
        "ack": "Mi allungo.",
        "done": "Stretch fatto.",
        "fail": "Stretch non riuscito.",
    },
    "sit": {
        "ack_key": "sit_ack",
        "done_key": "sit_done",
        "fail_key": "sit_fail",
        "ack": "Mi siedo.",
        "done": "Seduto.",
        "fail": "Non sono riuscito a sedermi.",
    },
    "recovery": {
        "ack_key": "recovery_ack",
        "done_key": "recovery_done",
        "fail_key": "recovery_fail",
        "ack": "Recovery.",
        "done": "Fatto.",
        "fail": "Recovery non riuscito.",
    },
}

# Chiavi file in go2_dashboard/hermes/canned/<key>.wav
CANNED_KEYS = sorted(
    {
        p["ack_key"]
        for p in PHRASES.values()
    }
    | {
        p["done_key"]
        for p in PHRASES.values()
    }
    | {
        p["fail_key"]
        for p in PHRASES.values()
    }
)

"""Memoria operatore / missione: append-only JSONL su disco (pose, note, snapshot contesto).

Separato da grasp_coach_memory: uso generico per dati che servono a ripresa sessioni o dataset."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT

_PATH = PROJECT_ROOT / "data" / "operator_session_memory.jsonl"
_LOCK = threading.Lock()


def operator_session_memory_path() -> Path:
    return _PATH


def append_operator_session_event(record: dict[str, Any]) -> None:
    rec = dict(record)
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    with _LOCK:
        with open(_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def read_recent_operator_session_events(max_lines: int = 120) -> list[dict[str, Any]]:
    if max_lines <= 0:
        return []
    if not _PATH.is_file():
        return []
    lines: list[str] = []
    with _LOCK:
        try:
            with open(_PATH, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return []
    tail = lines[-max_lines:]
    out: list[dict[str, Any]] = []
    for ln in tail:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out

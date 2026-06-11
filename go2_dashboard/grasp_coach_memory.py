"""Memoria incrementale per il Grasp Coach (JSON lines su disco, niente DB).

Ogni riga è un JSON con timestamp, istruzioni operatore, sintesi modello, stato esecuzione,
eventuali angoli servo dopo mossa (feedback). Usato come contesto rolling per OpenAI."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT

_MEMORY_PATH = PROJECT_ROOT / "data" / "grasp_coach_memory.jsonl"
_LOCK = threading.Lock()


def grasp_coach_memory_path() -> Path:
    return _MEMORY_PATH


def append_grasp_coach_event(record: dict[str, Any]) -> None:
    """Append una riga JSON (thread-safe)."""
    rec = dict(record)
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    with _LOCK:
        with open(_MEMORY_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def read_recent_grasp_coach_events(max_lines: int = 18) -> list[dict[str, Any]]:
    """Ultime N righe dal file (best-effort)."""
    if max_lines <= 0:
        return []
    if not _MEMORY_PATH.is_file():
        return []
    lines: list[str] = []
    with _LOCK:
        try:
            with open(_MEMORY_PATH, encoding="utf-8", errors="replace") as f:
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


def format_memory_for_prompt(events: list[dict[str, Any]], *, max_chars: int = 6000) -> str:
    """Testo compatto per system/user prompt OpenAI."""
    if not events:
        return "(no prior grasp-coach memory on this robot)"
    chunks: list[str] = []
    for ev in events:
        ts = str(ev.get("ts") or "")[:22]
        if str(ev.get("kind") or "") == "operator_feedback":
            fb = str(ev.get("feedback_text") or "")[:420]
            cc = str(ev.get("code_correction_note") or "").strip()
            rsi = ev.get("related_step_index")
            rrep = str(ev.get("related_assistant_reply_it") or "").strip()
            line = f"- [{ts}] OPERATOR_FEEDBACK (after coach step_index={rsi})\n  feedback: {fb}"
            if rrep:
                line += f"\n  coach_reply_then: {rrep[:240]}"
            if cc:
                line += f"\n  code_or_prompt_fix_request: {cc[:360]}"
            chunks.append(line)
            continue
        op = str(ev.get("operator_instruction") or "")[:220]
        rep = str(ev.get("assistant_reply_it") or "")[:260]
        ex = ev.get("executed")
        ok = ev.get("motion_ok")
        tip = ev.get("tool_tip_base_link_m_after")
        lbl = ev.get("pose_label")
        line = f"- [{ts}] exec={ex} ok={ok} label={lbl!r}\n  op: {op}\n  bot: {rep}"
        if isinstance(tip, list) and len(tip) >= 3:
            line += f"\n  tip_bl_m: [{tip[0]:.3f},{tip[1]:.3f},{tip[2]:.3f}]"
        chunks.append(line)
    text = "\n".join(chunks)
    if len(text) > max_chars:
        return text[-max_chars:]
    return text

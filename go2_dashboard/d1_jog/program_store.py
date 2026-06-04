"""Persistenza programmi a punti (JSON su disco NX)."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT

_PROGRAMS_DIR = Path(
    os.environ.get(
        "D1_PROGRAMS_DIR",
        str(PROJECT_ROOT / "data" / "d1_programs"),
    )
)


def _programs_dir() -> Path:
    d = _PROGRAMS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _program_path(program_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in program_id)
    return _programs_dir() / f"{safe}.json"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def list_programs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted(_programs_dir().glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out.append(
                {
                    "id": data.get("id", p.stem),
                    "name": data.get("name", p.stem),
                    "waypoint_count": len(data.get("waypoints") or []),
                    "updated_at": data.get("updated_at"),
                }
            )
        except (OSError, json.JSONDecodeError):
            continue
    return out


def load_program(program_id: str) -> dict[str, Any] | None:
    path = _program_path(program_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_program(data: dict[str, Any]) -> dict[str, Any]:
    pid = str(data.get("id") or uuid.uuid4().hex[:12])
    data["id"] = pid
    data.setdefault("created_at", _now_iso())
    data["updated_at"] = _now_iso()
    data.setdefault("waypoints", [])
    path = _program_path(pid)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def create_program(name: str) -> dict[str, Any]:
    return save_program({"id": uuid.uuid4().hex[:12], "name": name.strip() or "Programma", "waypoints": []})


def delete_program(program_id: str) -> bool:
    path = _program_path(program_id)
    if path.is_file():
        path.unlink()
        return True
    return False


def add_waypoint(
    program_id: str,
    *,
    name: str | None = None,
    servo_deg: list[float],
    tcp_pose: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    prog = load_program(program_id)
    if prog is None:
        return None, None
    wps: list[dict[str, Any]] = list(prog.get("waypoints") or [])
    idx = len(wps) + 1
    wp = {
        "id": f"wp-{uuid.uuid4().hex[:8]}",
        "name": (name or f"Punto {idx}").strip(),
        "servo_deg": [round(float(x), 3) for x in servo_deg[:7]],
        "tcp": tcp_pose,
        "saved_at": _now_iso(),
    }
    wps.append(wp)
    prog["waypoints"] = wps
    prog["updated_at"] = _now_iso()
    save_program(prog)
    return prog, wp


def delete_waypoint(program_id: str, waypoint_id: str) -> dict[str, Any] | None:
    prog = load_program(program_id)
    if prog is None:
        return None
    wps = [w for w in (prog.get("waypoints") or []) if w.get("id") != waypoint_id]
    prog["waypoints"] = wps
    prog["updated_at"] = _now_iso()
    return save_program(prog)


def rename_waypoint(program_id: str, waypoint_id: str, name: str) -> dict[str, Any] | None:
    prog = load_program(program_id)
    if prog is None:
        return None
    for w in prog.get("waypoints") or []:
        if w.get("id") == waypoint_id:
            w["name"] = name.strip() or w.get("name", "Punto")
            break
    prog["updated_at"] = _now_iso()
    return save_program(prog)


def find_waypoint_by_name_substr(
    name_substr: str,
    *,
    program_id: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Primo waypoint il cui nome contiene ``name_substr`` (case-insensitive)."""
    needle = (name_substr or "").strip().lower()
    if not needle:
        return None
    if program_id:
        prog = load_program(program_id)
        candidates: list[tuple[str, dict[str, Any] | None]] = [(program_id, prog)]
    else:
        candidates = [(meta["id"], load_program(meta["id"])) for meta in list_programs()]
    for pid, prog in candidates:
        if prog is None:
            continue
        for w in prog.get("waypoints") or []:
            if needle in str(w.get("name", "")).lower():
                return pid, w
    return None


def find_scan_waypoint() -> tuple[str, dict[str, Any]] | None:
    """Posa scansione dal programma salvato (env opzionale per programma / sottostringa nome)."""
    pid = (os.environ.get("D1_SCAN_PROGRAM_ID") or "").strip() or None
    substr = (os.environ.get("D1_SCAN_WAYPOINT_SUBSTR") or "scansione").strip()
    return find_waypoint_by_name_substr(substr, program_id=pid)

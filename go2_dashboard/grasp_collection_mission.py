"""Missione raccolta scatole colorate: cerca → presa autonoma → deposito → prossimo colore."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT

CONFIRM_TOKEN = "RUN_COLLECT_MISSION"

_JOB_LOCK = threading.Lock()
_JOB: dict[str, Any] = {
    "running": False,
    "flow": "collect_mission",
    "ok": None,
    "started_at": None,
    "finished_at": None,
    "current_step": None,
    "label_it": "Nessuna raccolta avviata.",
    "steps": [],
    "params": None,
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


def collect_mission_status() -> dict[str, Any]:
    with _JOB_LOCK:
        return json.loads(json.dumps({k: v for k, v in _JOB.items()}, default=str))


def _job_set(**kw: Any) -> None:
    with _JOB_LOCK:
        _JOB.update(kw)


def _job_step(step: dict[str, Any]) -> None:
    with _JOB_LOCK:
        _JOB["steps"].append(step)
        _JOB["current_step"] = step.get("step")


def _search_front_color(color: str, *, front_camera: int, timeout_s: float) -> dict[str, Any]:
    from go2_dashboard.grasp_full_sequence import _detect_on_camera

    deadline = time.time() + timeout_s
    last: dict[str, Any] = {"ok": False}
    while time.time() < deadline:
        det = _detect_on_camera(front_camera, color_hint=color)
        last = det
        if det.get("ok"):
            return {"ok": True, "detection": det}
        time.sleep(0.35)
    return {"ok": False, "reason": "front_search_timeout", "last": last}


def _turn_search_yaws(max_turns: int) -> None:
    from go2_dashboard.grasp_side_approach import _hold_velocity

    yaw_rate = _envf("GO2_COLLECT_SEARCH_YAW_RATE", 0.35)
    turn_s = _envf("GO2_COLLECT_SEARCH_TURN_S", 0.45)
    for _ in range(max(0, max_turns)):
        _hold_velocity(vx=0.0, vy=0.0, vyaw=yaw_rate, duration_s=turn_s)
        time.sleep(0.3)


def _goto_deposit() -> dict[str, Any]:
    from go2_dashboard.d1_jog import program_store, program_runner, service

    substr = (os.environ.get("D1_DEPOSIT_WAYPOINT_SUBSTR") or "deposito").strip().lower()
    found = program_store.find_waypoint_by_name_substr(substr)
    if found is None:
        return {"ok": False, "reason": "deposit_waypoint_not_found"}
    _pid, wp = found
    raw = wp.get("servo_deg")
    if not isinstance(raw, list) or len(raw) < 6:
        return {"ok": False, "reason": "invalid_deposit_waypoint"}
    servo = service.clamp_servo_deg([float(x) for x in raw[:7]])
    service._halt_cartesian_stream(wait_idle=True)
    couple = service.ensure_coupled_for_motion()
    if not couple.get("ok"):
        return {"ok": False, "reason": "not_coupled", "coupling": couple}
    out = program_runner.move_to_servo_deg_smooth(servo)
    out["waypoint_name"] = wp.get("name")
    return out


def run_collect_after_scan(
    *,
    targets: list[str],
    max_picks: int,
    instruction: str,
    front_camera: int,
    skip_scan: bool = False,
    on_progress: Any | None = None,
    on_log: Any | None = None,
    on_step: Any | None = None,
) -> dict[str, Any]:
    """Loop raccolta dopo eventuale scan j90 (usato anche dal flusso teach unificato)."""
    from go2_dashboard.grasp_autonomous_loop import _execute_autonomous_grasp

    picks = 0
    ok_mission = False
    last_grasp_verify: dict[str, Any] | None = None

    def _log(level: str, step: str, msg: str) -> None:
        if callable(on_log):
            on_log(level, step, msg)

    if not _truthy("GO2_ENABLE_BASE_MOTION", "0") and not _truthy("GO2_LOCAL", "0"):
        return {
            "ok": False,
            "picks_done": 0,
            "label_it": "Base motion disabilitato.",
            "last_grasp_verify": None,
        }

    search_timeout = _envf("GO2_COLLECT_SEARCH_TIMEOUT_S", 8.0)
    max_turns = int(_envf("GO2_COLLECT_SEARCH_MAX_TURNS", 8))

    for color in targets:
        if picks >= max_picks:
            break
        _log("info", "search", f"Cerco scatola {color}…")
        if callable(on_progress):
            on_progress(label_it=f"Cerco scatola {color}…", current_step=f"search_{color}")
        found = _search_front_color(color, front_camera=front_camera, timeout_s=search_timeout)
        if not found.get("ok"):
            _turn_search_yaws(max_turns)
            found = _search_front_color(color, front_camera=front_camera, timeout_s=search_timeout)
        step_entry = {"step": f"search_{color}", **found}
        if callable(on_step):
            on_step(step_entry)
        if not found.get("ok"):
            _log("warn", "search", f"Scatola {color} non trovata — passo al prossimo colore.")
            continue

        _log("info", "grasp", f"Presa autonoma {color}…")
        if callable(on_progress):
            on_progress(label_it=f"Presa autonoma {color}…", current_step=f"grasp_{color}")
        instr = instruction or f"prendi la scatola {color}"
        grasp_result = _execute_autonomous_grasp(
            instruction=instr,
            color_hint=color,
            max_cycles=int(_envf("GO2_GRASP_AUTONOMOUS_MAX_CYCLES", 20)),
            use_supervisor=_truthy("GO2_GRASP_COACH_SUPERVISOR", "1"),
            on_progress=on_progress,
        )
        gv = grasp_result.get("grasp_verify") if isinstance(grasp_result.get("grasp_verify"), dict) else {}
        last_grasp_verify = gv or last_grasp_verify
        grasp_ok = bool(grasp_result.get("grasp_detected"))
        grasp_step = {"step": f"grasp_{color}", "ok": grasp_ok, "grasp_result": grasp_result}
        if callable(on_step):
            on_step(grasp_step)
        if not grasp_ok:
            _log("error", "grasp", f"Presa {color} fallita.")
            continue

        picks += 1
        _log("info", "deposit", f"Deposito dopo presa {color} ({picks}/{max_picks})…")
        if callable(on_progress):
            on_progress(label_it=f"Deposito dopo presa {color}…", current_step=f"deposit_{color}")
        dep = _goto_deposit()
        dep_step = {"step": f"deposit_{color}", **dep}
        if callable(on_step):
            on_step(dep_step)
        try:
            from go2_dashboard.d1_arm_publish_lite import publish_move_one_joint_deg

            publish_move_one_joint_deg(6, 22.0)
        except Exception:
            pass
        ok_mission = True

    label = f"Raccolta finita — {picks} prese."
    return {
        "ok": ok_mission and picks > 0,
        "picks_done": picks,
        "label_it": label,
        "last_grasp_verify": last_grasp_verify,
        "skip_scan": skip_scan,
    }


def _run_collect_worker(
    *,
    targets: list[str],
    max_picks: int,
    instruction: str,
    front_camera: int,
) -> None:
    try:
        result = run_collect_after_scan(
            targets=targets,
            max_picks=max_picks,
            instruction=instruction,
            front_camera=front_camera,
            on_progress=lambda **kw: _job_set(
                label_it=kw.get("label_it", _JOB.get("label_it")),
                current_step=kw.get("current_step", _JOB.get("current_step")),
            ),
            on_step=_job_step,
        )
        _job_set(
            running=False,
            ok=bool(result.get("ok")),
            finished_at=_now_iso(),
            picks_done=int(result.get("picks_done") or 0),
            label_it=str(result.get("label_it") or "Raccolta terminata."),
        )
        try:
            p = PROJECT_ROOT / "data" / "grasp_collect_last.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(collect_mission_status(), indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    except Exception as exc:
        _job_set(running=False, ok=False, finished_at=_now_iso(), label_it=f"Errore raccolta: {exc!r}")


def start_collect_mission(
    *,
    targets: list[str] | None = None,
    instruction: str = "",
    confirm: str | None = None,
    max_picks: int | None = None,
    front_camera: int = 6,
) -> tuple[dict[str, Any], int]:
    with _JOB_LOCK:
        if _JOB.get("running"):
            return ({"ok": False, "reason": "job_already_running"}, 409)

    import sys

    s = str(PROJECT_ROOT / "scripts")
    if s not in sys.path:
        sys.path.insert(0, s)
    from box_object_detector import parse_color_from_instruction

    tgt = list(targets or [])
    if not tgt:
        parsed = parse_color_from_instruction(instruction)
        if parsed:
            tgt = [parsed]
        else:
            tgt = ["blu"]
    tgt = [str(t).strip().lower() for t in tgt if str(t).strip()]
    mp = max_picks if max_picks is not None else int(_envf("GO2_COLLECT_MAX_PICKS", 3))
    mp = max(1, min(mp, 12))

    if confirm != CONFIRM_TOKEN:
        return (
            {
                "ok": True,
                "started": False,
                "dry_run": True,
                "confirm_required": CONFIRM_TOKEN,
                "targets": tgt,
                "max_picks": mp,
                "hint_it": f"Dry-run — confirm={CONFIRM_TOKEN!r} per avviare.",
            },
            200,
        )

    _job_set(
        running=True,
        ok=None,
        started_at=_now_iso(),
        finished_at=None,
        steps=[],
        picks_done=0,
        params={"targets": tgt, "instruction": instruction, "max_picks": mp},
        label_it="Avvio raccolta…",
    )
    th = threading.Thread(
        target=_run_collect_worker,
        kwargs={
            "targets": tgt,
            "max_picks": mp,
            "instruction": instruction,
            "front_camera": front_camera,
        },
        name="grasp_collect",
        daemon=True,
    )
    th.start()
    return (
        {"ok": True, "started": True, "poll": "/api/grasp/collect_status", "status": collect_mission_status()},
        202,
    )

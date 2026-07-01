#!/usr/bin/env python3
"""Esegue ciclo presa come UI: teach_cancel → teach_run → poll + monitor pid Flask."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://192.168.123.18:5052"
DBG = Path(__file__).resolve().parents[1] / "debug-fdb211.log"
INSTRUCTION = "prendi la scatola"
TIMEOUT_S = 360


def _dbg(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    # #region agent log
    try:
        with DBG.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "sessionId": "fdb211",
                        "hypothesisId": hypothesis_id,
                        "location": location,
                        "message": message,
                        "data": data or {},
                        "timestamp": int(time.time() * 1000),
                        "runId": "ui-like",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass
    # #endregion


def _get(path: str, timeout: float = 20.0) -> tuple[int, dict]:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8", errors="replace") or "{}")


def _post(path: str, body: dict, timeout: float = 60.0) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8", errors="replace") or "{}")


def main() -> int:
    print("=== UI-like grasp teach ===")
    try:
        st, health = _get("/api/health", timeout=10)
        pid0 = health.get("pid")
        print("health", st, "pid", pid0)
        _dbg("H0", "ui_like_grasp.py", "health", {"pid": pid0, "ok": health.get("ok")})
    except Exception as exc:
        print("FAIL health:", exc)
        _dbg("H0", "ui_like_grasp.py", "health_fail", {"error": str(exc)})
        return 10

    try:
        _post("/api/grasp/teach_cancel", {"reason_it": "ui_like_grasp reset"}, timeout=15)
    except Exception:
        pass

    print("POST teach_run (come pulsante Prendi)")
    try:
        code, body = _post(
            "/api/grasp/teach_run",
            {"confirm": "RUN_TEACH_GRASP", "instruction": INSTRUCTION, "max_cycles": 2},
            timeout=90,
        )
    except urllib.error.HTTPError as he:
        raw = he.read().decode("utf-8", errors="replace")
        print("teach_run HTTP", he.code, raw[:500])
        _dbg("H2", "ui_like_grasp.py", "teach_run_http_err", {"code": he.code, "raw": raw[:300]})
        return 2

    print("teach_run", code, json.dumps({k: body.get(k) for k in ("started", "mode", "reason")}))
    _dbg("H2", "ui_like_grasp.py", "teach_run", {"http": code, **{k: body.get(k) for k in ("started", "mode", "reason")}})
    if not body.get("started"):
        return 2

    deadline = time.time() + TIMEOUT_S
    last_step = None
    pid_changes = 0
    last_pid = pid0

    while time.time() < deadline:
        try:
            _, h = _get("/api/health", timeout=8)
            pid = h.get("pid")
            if pid != last_pid:
                pid_changes += 1
                _dbg("H6", "ui_like_grasp.py", "flask_pid_changed", {"from": last_pid, "to": pid})
                print(f"WARN Flask pid changed {last_pid} -> {pid}")
                last_pid = pid
        except Exception as exc:
            _dbg("H6", "ui_like_grasp.py", "health_poll_fail", {"error": str(exc)})

        try:
            _, st = _get("/api/grasp/teach_status", timeout=15)
        except Exception as exc:
            print("teach_status err", exc)
            time.sleep(2)
            continue

        step = st.get("current_step")
        running = st.get("running")
        if step != last_step or not running:
            line = (
                f"  step={step} running={running} pct={st.get('progress_pct')} "
                f"ok={st.get('ok')} — {st.get('label_it')}"
            )
            print(line)
            _dbg(
                "H5",
                "ui_like_grasp.py",
                "teach_status",
                {
                    "step": step,
                    "running": running,
                    "pct": st.get("progress_pct"),
                    "ok": st.get("ok"),
                    "failed_step": st.get("failed_step"),
                    "label_it": st.get("label_it"),
                    "pid": last_pid,
                },
            )
            lines = st.get("log_lines") or []
            if lines:
                print("   log:", lines[-1].get("msg_it", ""))
            last_step = step

        if not running and st.get("started_at"):
            print("=== FINE ===")
            print("ok=", st.get("ok"), "failed=", st.get("failed_step"))
            for s in st.get("steps") or []:
                print(f"  {s.get('id')}: {s.get('status')} — {s.get('detail', '')[:80]}")
            _dbg(
                "H5",
                "ui_like_grasp.py",
                "final",
                {
                    "ok": st.get("ok"),
                    "failed_step": st.get("failed_step"),
                    "pid_changes": pid_changes,
                    "steps": st.get("steps"),
                },
            )
            return 0 if st.get("ok") else 3

        if not running and not st.get("started_at") and pid_changes > 0:
            print("=== RESET (Flask restart?) ===")
            _dbg("H6", "ui_like_grasp.py", "job_reset_mid_run", {"pid_changes": pid_changes})
            return 4

        time.sleep(2)

    print("TIMEOUT")
    _dbg("H5", "ui_like_grasp.py", "timeout", {"pid_changes": pid_changes})
    return 5


if __name__ == "__main__":
    raise SystemExit(main())

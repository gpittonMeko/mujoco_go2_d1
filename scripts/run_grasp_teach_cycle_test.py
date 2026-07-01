#!/usr/bin/env python3
"""Esegue un giro completo grasp teach sulla NX e verifica che non resti bloccato."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request


def _get(base: str, path: str) -> dict:
    req = urllib.request.Request(base + path, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}


def _post(base: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return resp.status, json.loads(raw) if raw.strip() else {}


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://192.168.123.18:5052").rstrip("/")
    instruction = sys.argv[2] if len(sys.argv) > 2 else "prendi la scatola blu"
    timeout_s = int(sys.argv[3]) if len(sys.argv) > 3 else 600

    print("=== STATO INIZIALE ===")
    for path in (
        "/api/grasp/teach_status",
        "/api/grasp/autonomous_status",
        "/api/grasp/collect_status",
        "/api/health",
    ):
        try:
            st = _get(base, path)
            keys = {
                k: st.get(k)
                for k in (
                    "running",
                    "ok",
                    "current_step",
                    "failed_step",
                    "label_it",
                    "progress_pct",
                    "started_at",
                    "finished_at",
                )
                if k in st
            }
            print(path, json.dumps(keys, ensure_ascii=False))
        except Exception as exc:
            print(path, "ERR", exc)

    print("\n=== CANCEL preflight ===")
    _, cancel = _post(base, "/api/grasp/teach_cancel", {"reason_it": "reset test agente"})
    print(
        "cancel:",
        cancel.get("was_running"),
        cancel.get("cancelled"),
        (cancel.get("status") or {}).get("label_it"),
    )

    print("\n=== AVVIO teach_run ===")
    try:
        code, body = _post(
            base,
            "/api/grasp/teach_run",
            {
                "confirm": "RUN_TEACH_GRASP",
                "instruction": instruction,
                "max_cycles": 3,
            },
        )
    except urllib.error.HTTPError as he:
        raw = he.read().decode("utf-8", errors="replace")
        print("HTTP", he.code, raw[:500])
        return 1

    print(
        "HTTP",
        code,
        "started=",
        body.get("started"),
        "mode=",
        body.get("mode"),
        "reason=",
        body.get("reason"),
    )
    if not body.get("started"):
        print("FAIL start:", json.dumps(body, ensure_ascii=False)[:800])
        return 2

    deadline = time.time() + timeout_s
    last_step = None
    last_pct = -1
    stall_since: float | None = None
    stall_step: str | None = None

    while time.time() < deadline:
        st = _get(base, "/api/grasp/teach_status")
        step = st.get("current_step")
        pct = st.get("progress_pct")
        running = st.get("running")
        if step != last_step or pct != last_pct:
            print(
                f"  [{time.strftime('%H:%M:%S')}] running={running} step={step} "
                f"pct={pct} ok={st.get('ok')} label={st.get('label_it')}"
            )
            lines = st.get("log_lines") or []
            if lines:
                print("    log:", lines[-1].get("msg_it", ""))
            last_step, last_pct = step, pct
            stall_since = time.time()
            stall_step = step
        elif running and stall_since and (time.time() - stall_since) > 120:
            print(
                f"WARN stall >120s su step={stall_step} pct={pct} "
                f"label={st.get('label_it')}"
            )
            stall_since = time.time()

        if not running:
            print("\n=== FINE FLUSSO ===")
            print("ok=", st.get("ok"), "failed_step=", st.get("failed_step"))
            print("label=", st.get("label_it"))
            for s in st.get("steps") or []:
                print(
                    f"  step {s.get('id')}: {s.get('status')} — {s.get('detail', '')}"
                )
            if st.get("ok"):
                print("GRASP_TEACH_CYCLE_OK")
                return 0
            print("GRASP_TEACH_CYCLE_FAIL")
            return 3

        time.sleep(2)

    print(f"TIMEOUT {timeout_s}s — possibile blocco UI")
    st = _get(base, "/api/grasp/teach_status")
    print(
        "stale status:",
        json.dumps(
            {
                k: st.get(k)
                for k in (
                    "running",
                    "current_step",
                    "progress_pct",
                    "label_it",
                    "started_at",
                )
            },
            ensure_ascii=False,
        ),
    )
    return 4


if __name__ == "__main__":
    raise SystemExit(main())

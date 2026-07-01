#!/usr/bin/env python3
"""Profila tempi grasp teach: rete PC, API NX, Orbbec preview, ciclo completo."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request


def _get(base: str, path: str, timeout: float = 20) -> tuple[float, dict]:
    t0 = time.perf_counter()
    req = urllib.request.Request(base + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        dt = time.perf_counter() - t0
        return dt, json.loads(raw) if raw.strip() else {}


def _post(base: str, path: str, body: dict | None = None, timeout: float = 60) -> tuple[float, int, dict]:
    t0 = time.perf_counter()
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        dt = time.perf_counter() - t0
        return dt, resp.status, json.loads(raw) if raw.strip() else {}


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://192.168.123.18:5052").rstrip("/")
    print(f"BASE {base}\n")

    for label, path in [
        ("health", "/api/health"),
        ("teach_status", "/api/grasp/teach_status"),
        ("cameras", "/api/cameras/status"),
        ("coach", "/api/grasp_coach/status"),
    ]:
        try:
            dt, body = _get(base, path)
            print(f"GET {label:14} {dt*1000:7.0f} ms  keys={list(body.keys())[:6]}")
        except Exception as exc:
            print(f"GET {label:14} FAIL {exc}")

    print("\n--- Orbbec metric preview (solo acquisizione+detect, no braccio) ---")
    try:
        dt, code, body = _post(
            base,
            "/api/grasp_coach/preview",
            {"instruction": "prendi la scatola blu", "execute": False},
            timeout=90,
        )
        mg = body.get("metric_grounding") or {}
        print(
            f"POST preview     {dt*1000:7.0f} ms HTTP={code} ok={body.get('ok')} "
            f"reason={body.get('reason')} detect={mg.get('ok')} depth={mg.get('depth_m')}"
        )
        if body.get("_http_timing_ms"):
            print(f"  server _http_timing_ms={body.get('_http_timing_ms')}")
    except Exception as exc:
        print(f"POST preview     FAIL {exc}")

    print("\n--- teach_run + poll step transitions ---")
    _post(base, "/api/grasp/teach_cancel", {}, timeout=15)
    t_run, code, body = _post(
        base,
        "/api/grasp/teach_run",
        {"confirm": "RUN_TEACH_GRASP", "instruction": "prendi la scatola blu", "max_cycles": 2},
        timeout=60,
    )
    print(f"POST teach_run    {t_run*1000:7.0f} ms HTTP={code} started={body.get('started')}")
    if not body.get("started"):
        print(json.dumps(body, ensure_ascii=False)[:500])
        return 1

    t_start = time.perf_counter()
    last_step = None
    last_t = t_start
    transitions: list[tuple[str, float, str]] = []
    deadline = t_start + 300

    while time.perf_counter() < deadline:
        dt, st = _get(base, "/api/grasp/teach_status?_=" + str(time.time()), timeout=15)
        step = st.get("current_step")
        now = time.perf_counter()
        if step != last_step:
            transitions.append((step or "?", now - last_t, st.get("label_it") or ""))
            last_step = step
            last_t = now
            print(
                f"  +{transitions[-1][1]:6.1f}s step={step!r} poll={dt*1000:.0f}ms | "
                f"{(st.get('label_it') or '')[:70]}"
            )
        if not st.get("running"):
            total = now - t_start
            print(f"\nDONE total_worker={total:.1f}s ok={st.get('ok')} failed={st.get('failed_step')}")
            print("\nSTEP TIMING:")
            for name, sec, lbl in transitions:
                print(f"  {sec:6.1f}s  {name:10}  {lbl[:60]}")
            print("\nLAST LOGS:")
            for ln in (st.get("log_lines") or [])[-12:]:
                print(f"  [{ln.get('level')}] {ln.get('msg_it')}")
            cycles = st.get("autonomous_cycles")
            if isinstance(cycles, list):
                print("\nCYCLE DETAIL:")
                for c in cycles:
                    mot = c.get("motion") if isinstance(c.get("motion"), dict) else {}
                    print(
                        f"  {c.get('step')} stage={c.get('coach_stage')} "
                        f"motion_ok={mot.get('ok')} partials={mot.get('partial_count')} "
                        f"dist={c.get('dist_to_grasp_m')}"
                    )
            return 0 if st.get("ok") else 2
        time.sleep(0.8)

    print("TIMEOUT 300s")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())

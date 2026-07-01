#!/usr/bin/env python3
"""Smoke HTTP per flusso Presa teach / autonomo (no movimento robot)."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def _get(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return resp.status, json.loads(raw) if raw.strip() else {}


def _post(url: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return resp.status, json.loads(raw) if raw.strip() else {}


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://192.168.123.18:5052").rstrip("/")
    ok = True
    checks = [
        ("/api/grasp_coach/status", "grasp_coach"),
        ("/api/grasp_coach/teach_calib/status", "teach_calib"),
        ("/api/grasp/autonomous_status", "autonomous_status"),
        ("/api/grasp/collect_status", "collect_status"),
        ("/api/grasp/teach_status", "teach_status"),
    ]
    for path, name in checks:
        try:
            code, body = _get(base + path)
            print(f"OK {name} HTTP {code} keys={list(body.keys())[:8]}")
        except Exception as exc:
            ok = False
            print(f"FAIL {name}: {exc}")
    try:
        code, body = _get(base + "/api/presets/scan")
        print(f"scan preset HTTP {code} ok={body.get('ok')}")
    except urllib.error.HTTPError as he:
        if he.code == 404:
            print("WARN scan waypoint not configured (404) — salva SCANSIONE 90 in programma")
        else:
            ok = False
            print(f"FAIL scan preset: HTTP {he.code}")
    except Exception as exc:
        ok = False
        print(f"FAIL scan preset: {exc}")
    try:
        code0, _cancel = _post(base + "/api/grasp/teach_cancel", {})
        print(f"teach_cancel preflight HTTP {code0} was_running={_cancel.get('was_running')}")
        code, body = _post(base + "/api/grasp/teach_run", {"instruction": "prendi la scatola blu"})
        print(f"teach_run dry-run HTTP {code} started={body.get('started')} mode={body.get('mode')}")
        if not body.get("ok") or body.get("started") is not False:
            ok = False
            print(f"FAIL teach_run dry-run: {body}")
        code2, st = _get(base + "/api/grasp/teach_status")
        if "steps" not in st or "log_lines" not in st:
            ok = False
            print(f"FAIL teach_status schema: keys={list(st.keys())}")
        else:
            print(f"OK teach_status HTTP {code2} progress={st.get('progress_pct')}")
    except Exception as exc:
        ok = False
        print(f"FAIL teach_run/teach_status: {exc}")
    try:
        code3, cancel = _post(base + "/api/grasp/teach_cancel", {})
        print(
            f"teach_cancel HTTP {code3} cancelled={cancel.get('cancelled')} was_running={cancel.get('was_running')}"
        )
        if not cancel.get("ok"):
            ok = False
            print(f"FAIL teach_cancel: {cancel}")
    except Exception as exc:
        ok = False
        print(f"FAIL teach_cancel: {exc}")
    print("VERIFY_GRASP_TEACH_OK" if ok else "VERIFY_GRASP_TEACH_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

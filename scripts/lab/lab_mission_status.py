#!/usr/bin/env python3
"""Legge la mission console dalla dashboard operator (NX o localhost).

Uso (PC di lab sulla LAN Unitree):
  python scripts/lab/lab_mission_status.py
  python scripts/lab/lab_mission_status.py http://192.168.123.18:5052

Env: GO2_MISSION_CONSOLE_URL (default http://192.168.123.18:5052)
Exit code 0 se ok e (se proxy grasp attivo) worker raggiungibile; altrimenti 1.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    base = (
        (sys.argv[1] if len(sys.argv) > 1 else None)
        or os.environ.get("GO2_MISSION_CONSOLE_URL")
        or "http://192.168.123.18:5052"
    ).strip().rstrip("/")
    url = base + "/api/mission/console"
    print("GET", url)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = resp.getcode() or 200
    except urllib.error.URLError as exc:
        print("FAIL:", exc)
        return 1
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        print("FAIL: JSON decode", raw[:400])
        return 1
    print(json.dumps(data, indent=2, ensure_ascii=False)[:12000])
    if not data.get("ok"):
        print("LAB_MISSION_STATUS_FAIL", file=sys.stderr)
        return 1
    summary = data.get("summary") or {}
    gw = summary.get("grasp_worker") or {}
    if gw.get("proxy_enabled") and not gw.get("ok_for_plan"):
        print("LAB_MISSION_STATUS_FAIL: worker grasp non raggiungibile (proxy)", file=sys.stderr)
        return 1
    print("LAB_MISSION_STATUS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

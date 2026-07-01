#!/usr/bin/env python3
"""Verifica POST /api/pick/snapshot (Pick teach) — non deve killare Flask.

Uso:
  python scripts/verify_pick_snapshot_http.py http://192.168.123.18:5052
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def _post_json(url: str, body: dict, *, timeout_s: float = 90.0) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = int(getattr(resp, "status", resp.getcode()))
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        raw = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise SystemExit(f"VERIFY_FAIL: POST {url} — {exc!r} (Flask morto o rete?)") from exc
    try:
        return code, json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"VERIFY_FAIL: risposta non JSON (HTTP {code}): {raw[:400]}") from exc


def _get_health(base: str) -> dict:
    url = base.rstrip("/") + "/api/health"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        raise SystemExit(f"VERIFY_FAIL: GET {url} — {exc!r}") from exc


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://192.168.123.18:5052").strip().rstrip("/")
    h0 = _get_health(base)
    pid0 = h0.get("pid")
    print(f"OK health pid={pid0}")

    code, body = _post_json(base + "/api/pick/snapshot", {})
    print(f"POST /api/pick/snapshot HTTP {code} ok={body.get('ok')} reason={body.get('reason')}")
    if body.get("capture"):
        print(" capture.via:", (body.get("capture") or {}).get("via"))

    h1 = _get_health(base)
    pid1 = h1.get("pid")
    if pid1 != pid0:
        raise SystemExit(
            f"VERIFY_FAIL: Flask riavviato (pid {pid0} → {pid1}) — pick/snapshot ha ancora ucciso il server"
        )
    if code >= 500 and body.get("reason") == "pick_snapshot_error":
        raise SystemExit(f"VERIFY_FAIL: eccezione server — {body.get('error')}")
    print("VERIFY_OK pick/snapshot non ha killato Flask")


if __name__ == "__main__":
    main()

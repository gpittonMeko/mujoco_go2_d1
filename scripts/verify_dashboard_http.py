#!/usr/bin/env python3
"""
Verifica da questo PC che la dashboard HTTP risponda (stessa LAN del robot).

Uso (PowerShell / bash):
  python scripts/verify_dashboard_http.py http://192.168.123.18:5050

Exit 0 se /api/health e la home rispondono come atteso; altrimenti exit 1 con dettaglio errore.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def _get(url: str, limit: int) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        return int(resp.status), resp.read(limit)


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5050").rstrip("/")
    errs: list[str] = []

    health_url = f"{base}/api/health"
    try:
        code, body = _get(health_url, 8192)
        if code != 200:
            errs.append(f"{health_url} status {code}")
        else:
            j = json.loads(body.decode("utf-8", errors="replace"))
            if not j.get("ok") or j.get("service") != "go2_dashboard":
                errs.append(f"{health_url} JSON inatteso: {j!r}")
            else:
                print("OK", health_url, "pid=", j.get("pid"), "started=", j.get("process_started_at"))
    except urllib.error.HTTPError as exc:
        errs.append(f"{health_url} HTTPError {exc.code} {exc.reason}")
    except urllib.error.URLError as exc:
        errs.append(f"{health_url} URLError {exc.reason!r} — host raggiungibile? Firewall? Flask avviato con bind 0.0.0.0?")
    except Exception as exc:
        errs.append(f"{health_url} {type(exc).__name__}: {exc}")

    home_url = f"{base}/"
    try:
        code, body = _get(home_url, 65536)
        if code != 200:
            errs.append(f"{home_url} status {code}")
        elif b"Go2 Diagnostics Dashboard" not in body:
            errs.append(f"{home_url} body senza titolo atteso (dashboard vecchia o pagina sbagliata?)")
        else:
            print("OK", home_url, "bytes", len(body))
    except urllib.error.HTTPError as exc:
        errs.append(f"{home_url} HTTPError {exc.code} {exc.reason}")
    except urllib.error.URLError as exc:
        errs.append(f"{home_url} URLError {exc.reason!r}")
    except Exception as exc:
        errs.append(f"{home_url} {type(exc).__name__}: {exc}")

    if errs:
        print("VERIFY_FAIL:", file=sys.stderr)
        for e in errs:
            print(" ", e, file=sys.stderr)
        return 1
    print("VERIFY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

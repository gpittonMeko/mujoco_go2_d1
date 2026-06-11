#!/usr/bin/env python3
"""
Smoke test **solo lettura** verso la dashboard sulla NX (nessun comando Sport al cane).

Uso dalla root del repo (PC sulla LAN Unitree):
  python scripts/verify_nx_dashboard_apis.py http://192.168.123.18:5052
  # oppure: python scripts/verify_go2_lab.py dashboard-nx http://192.168.123.18:5052

Include ``GET /api/base/sport_connectivity`` (MotionSwitcher ``CheckMode``, stesso DDS della Sport API).

Env:
  VERIFY_NX_REQUIRE_DDS=1 — exit 1 se il probe DDS fallisce (oltre agli altri check).

Exit 0 se gli endpoint rispondono come atteso; 1 altrimenti.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def _get(url: str, timeout: float = 12.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.status), resp.read(65536)


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://192.168.123.18:5052").rstrip("/")
    errs: list[str] = []

    checks: list[tuple[str, str]] = [
        ("/api/health", "health"),
        ("/api/base/sport_env", "sport_env"),
        ("/api/base/sport_last", "sport_last"),
        ("/api/status", "status"),
    ]

    for path, label in checks:
        url = base + path
        try:
            code, body = _get(url)
            if code != 200:
                errs.append(f"{label}: HTTP {code}")
                continue
            j = json.loads(body.decode("utf-8", errors="replace"))
            if label == "status":
                if not isinstance(j.get("tests"), dict) or "summary" not in j:
                    errs.append(f"{label}: JSON status inatteso {j!r}")
                else:
                    print(f"OK {label} summary={(j.get('summary') or '')[:72]}")
                continue
            if not j.get("ok"):
                errs.append(f"{label}: ok!=true {j!r}")
                continue
            extra = ""
            if label == "health":
                extra = f" pid={j.get('pid')}"
            elif label == "sport_env":
                extra = f" dds={j.get('dds_interface_env')} domain={j.get('dds_domain')}"
            print(f"OK {label}{extra}")
        except urllib.error.HTTPError as exc:
            errs.append(f"{label}: HTTPError {exc.code}")
        except urllib.error.URLError as exc:
            errs.append(f"{label}: URLError {exc.reason!r}")
        except json.JSONDecodeError as exc:
            errs.append(f"{label}: JSON {exc}")
        except Exception as exc:
            errs.append(f"{label}: {type(exc).__name__}: {exc}")

    if errs:
        print("VERIFY_NX_APIS_FAIL:", file=sys.stderr)
        for e in errs:
            print(" ", e, file=sys.stderr)
        return 1

    strict_dds = os.environ.get("VERIFY_NX_REQUIRE_DDS", "0").lower() in {"1", "true", "yes"}
    conn_url = base + "/api/base/sport_connectivity"
    try:
        req = urllib.request.Request(conn_url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=22) as resp:
            ccode = int(resp.status)
            cbody = resp.read(65536)
        cj = json.loads(cbody.decode("utf-8", errors="replace"))
        if ccode == 403:
            print("SKIP sport_connectivity (non è GO2_LOCAL sulla dashboard)")
        elif cj.get("ok"):
            mm = cj.get("motion_mode_result")
            cc = cj.get("motion_switcher_check_code")
            print(f"OK sport_connectivity CheckMode code={cc} motion_mode={mm!r}")
        else:
            msg = f"sport_connectivity HTTP {ccode} body={cj!r}"
            print("WARN", msg)
            if strict_dds:
                print("VERIFY_NX_APIS_FAIL:", file=sys.stderr)
                print(" ", msg, file=sys.stderr)
                return 1
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            cj = json.loads(raw) if raw else {}
        except Exception:
            cj = {}
        msg = f"sport_connectivity HTTPError {exc.code} {cj!r}"
        print("WARN", msg)
        if strict_dds:
            print("VERIFY_NX_APIS_FAIL:", file=sys.stderr)
            print(" ", msg, file=sys.stderr)
            return 1
    except urllib.error.URLError as exc:
        msg = f"sport_connectivity URLError {exc.reason!r}"
        print("WARN", msg)
        if strict_dds:
            print("VERIFY_NX_APIS_FAIL:", file=sys.stderr)
            print(" ", msg, file=sys.stderr)
            return 1
    except Exception as exc:
        msg = f"sport_connectivity {type(exc).__name__}: {exc}"
        print("WARN", msg)
        if strict_dds:
            print("VERIFY_NX_APIS_FAIL:", file=sys.stderr)
            print(" ", msg, file=sys.stderr)
            return 1

    print("VERIFY_NX_APIS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

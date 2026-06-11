#!/usr/bin/env python3
"""Smoke test Grasp Coach da PC verso dashboard sulla NX.

Verifica:
  GET  /api/grasp_coach/status
  POST /api/grasp_coach/step (default: solo pianificazione OpenAI, ``execute=false`` — niente IK/DDS).

Per test braccio reale sulla NX usa tab Presa → Grasp Coach con **Esegui mossa D1**,
oppure: ``python scripts/verify_grasp_coach_http.py URL --execute`` (ATTENZIONE: muove il braccio).

Base Go2 (Sport): il Grasp Coach non comanda le zampe salvo ``GO2_GRASP_COACH_BALANCE_HOLD_FIRST=1``
sulla NX (prima di IK chiama ``balance_hold`` se ``GO2_ENABLE_BASE_MOTION=1``).

Uso:
  python scripts/verify_grasp_coach_http.py http://192.168.123.18:5052
  python scripts/verify_grasp_coach_http.py http://192.168.123.18:5052 --step
  python scripts/verify_grasp_coach_http.py http://192.168.123.18:5052 --step --execute
  python scripts/verify_grasp_coach_http.py http://192.168.123.18:5052 --step --feedback
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def _req_json(
    method: str, url: str, body: dict | None = None, timeout_s: float = 45.0
) -> tuple[int, dict, dict[str, str]]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        client_ms = (time.monotonic() - t0) * 1000.0
        hdr = {k.lower(): v for k, v in resp.headers.items()}
        hdr["_client_round_trip_ms"] = f"{client_ms:.2f}"
        return int(resp.status), json.loads(raw) if raw.strip() else {}, hdr


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify grasp coach HTTP on dashboard")
    ap.add_argument("base", nargs="?", default="http://127.0.0.1:5052", help="Dashboard base URL")
    ap.add_argument("--step", action="store_true", help="POST one coach step (uses OpenAI key on NX)")
    ap.add_argument(
        "--execute",
        action="store_true",
        help="If combined with --step, set execute=true (real arm IK via DDS — dangerous)",
    )
    ap.add_argument(
        "--instruction",
        default="Briefly describe what you see in RGB; propose a small partial approach target or null if unsure.",
        help="Operator instruction for coach step",
    )
    ap.add_argument(
        "--feedback",
        action="store_true",
        help="After a successful --step, POST /api/grasp_coach/feedback (append-only memory test)",
    )
    args = ap.parse_args()
    base = str(args.base).rstrip("/")

    status_url = f"{base}/api/grasp_coach/status"
    try:
        code, j, hdr = _req_json("GET", status_url, timeout_s=12.0)
    except Exception as exc:
        print("FAIL", status_url, exc, file=sys.stderr)
        return 1
    if code != 200:
        print("FAIL", status_url, "HTTP", code, file=sys.stderr)
        return 1
    print("OK", status_url)
    print(
        "--- HTTP timing ---",
        f"client_round_trip_ms: {hdr.get('_client_round_trip_ms', '?')}",
        f"server_process_ms (header): {hdr.get('x-dashboard-server-ms', '—')}",
        sep="\n",
    )
    print(json.dumps(j, indent=2, ensure_ascii=False))

    if not args.step:
        print("(skip POST step; pass --step to call OpenAI)")
        return 0

    step_url = f"{base}/api/grasp_coach/step"
    body = {
        "instruction": args.instruction,
        "execute": bool(args.execute),
        "include_depth": True,
        "logical_camera_rgb": 0,
        "session_note": "verify_grasp_coach_http.py smoke test",
        "step_index": 0,
    }
    try:
        code2, j2, h2 = _req_json("POST", step_url, body=body, timeout_s=55.0)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        print("FAIL", step_url, exc.code, err_body[:1200], file=sys.stderr)
        return 1
    except Exception as exc:
        print("FAIL", step_url, exc, file=sys.stderr)
        return 1

    print("OK", step_url, "HTTP", code2)
    print(
        "--- HTTP timing ---",
        f"client_round_trip_ms: {h2.get('_client_round_trip_ms', '?')}",
        f"server_process_ms (header): {h2.get('x-dashboard-server-ms', '—')}",
        sep="\n",
    )
    print(json.dumps(j2, indent=2, ensure_ascii=False))
    if not j2.get("ok"):
        return 1
    if args.execute and j2.get("motion") and not j2["motion"].get("ok"):
        print("WARN: execute requested but motion.ok is false", file=sys.stderr)
        return 1

    if args.feedback:
        fb_url = f"{base}/api/grasp_coach/feedback"
        fb_body = {
            "feedback_text": "verify_grasp_coach_http.py: test feedback dopo step OK.",
            "code_correction_note": "Esempio nota sviluppatore / tweak prompt.",
            "related_step_index": j2.get("step_index"),
            "related_assistant_reply_it": (j2.get("assistant_reply_it") or "")[:400],
        }
        try:
            code3, j3, h3 = _req_json("POST", fb_url, body=fb_body, timeout_s=15.0)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            print("FAIL", fb_url, exc.code, err_body[:1200], file=sys.stderr)
            return 1
        except Exception as exc:
            print("FAIL", fb_url, exc, file=sys.stderr)
            return 1
        print("OK", fb_url, "HTTP", code3)
        print(
            "--- HTTP timing ---",
            f"client_round_trip_ms: {h3.get('_client_round_trip_ms', '?')}",
            f"server_process_ms (header): {h3.get('x-dashboard-server-ms', '—')}",
            sep="\n",
        )
        print(json.dumps(j3, indent=2, ensure_ascii=False))
        if not j3.get("ok"):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

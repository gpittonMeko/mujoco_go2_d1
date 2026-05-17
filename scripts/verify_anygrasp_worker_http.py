#!/usr/bin/env python3
"""Verifica HTTP del worker AnyGrasp (stesso contratto usato da go2_dashboard/blueprints/grasp.py).

Uso da PC o dalla NX:
  python scripts/verify_anygrasp_worker_http.py
  python scripts/verify_anygrasp_worker_http.py http://192.168.123.20:8765

Env: GO2_ANYGRASP_WORKER_URL (default http://127.0.0.1:8765)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GO2_ANYGRASP_WORKER_URL") or "http://127.0.0.1:8765").strip().rstrip("/")
    print("worker_base:", base)

    def get(path: str) -> tuple[int, dict]:
        req = urllib.request.Request(base + path, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = resp.getcode() or 200
            try:
                return code, json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                return code, {"_raw": raw[:800]}

    def post(path: str, body: dict) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            base + path,
            data=data,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = resp.getcode() or 200
            try:
                return code, json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                return code, {"_raw": raw[:800]}

    try:
        code, j = get("/health")
        print("GET /health ->", code, json.dumps(j, ensure_ascii=False)[:900])
    except urllib.error.URLError as e:
        print("FAIL GET /health:", e)
        return 1

    try:
        code, j = post("/plan", {})
        print("POST /plan ->", code, json.dumps(j, ensure_ascii=False)[:900])
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:600]
        print("POST /plan -> HTTP", e.code, body)
    except urllib.error.URLError as e:
        print("FAIL POST /plan:", e)
        return 1

    print("VERIFY_ANYGRASP_WORKER_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

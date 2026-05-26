#!/usr/bin/env python3
"""Verifica worker VLA su AWS (health + POST /plan con JPEG locali o stub).

Uso:
  python scripts/verify_aws_vla_worker.py https://<host-o-alb>:8765
  python scripts/verify_aws_vla_worker.py https://<host> --token <GO2_WORKER_TOKEN>
  python scripts/verify_aws_vla_worker.py https://<host> --dual-jpeg cam0.jpg cam6.jpg

Env: GO2_WORKER_TOKEN, GO2_ANYGRASP_WORKER_URL
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _post_json(url: str, body: dict, token: str | None, timeout_s: float) -> tuple[dict, int]:
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["X-Worker-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return (json.loads(raw) if raw.strip() else {"ok": True}), resp.getcode() or 200
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(err_body) if err_body.strip() else {"ok": False}
        except json.JSONDecodeError:
            payload = {"ok": False, "body": err_body[:800]}
        return payload, exc.code


def _get_json(url: str, token: str | None, timeout_s: float) -> tuple[dict, int]:
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Worker-Token"] = token
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return (json.loads(raw) if raw.strip() else {"ok": True}), resp.getcode() or 200
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(err_body) if err_body.strip() else {"ok": False}
        except json.JSONDecodeError:
            payload = {"ok": False, "body": err_body[:800]}
        return payload, exc.code


def _load_jpeg_b64(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("ascii")


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify AWS VLA worker HTTP")
    ap.add_argument("base_url", nargs="?", default=os.environ.get("GO2_ANYGRASP_WORKER_URL") or "")
    ap.add_argument("--token", default=os.environ.get("GO2_WORKER_TOKEN") or "")
    ap.add_argument("--dual-jpeg", nargs=2, metavar=("WRIST", "FRONT"), default=None)
    ap.add_argument("--instruction", default="afferra l'oggetto davanti al braccio")
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args()

    base = (args.base_url or "").strip().rstrip("/")
    if not base:
        print("Usage: verify_aws_vla_worker.py https://host:8765", file=sys.stderr)
        return 2

    token = (args.token or "").strip() or None

    health_url = base + "/health"
    print(f"GET {health_url}")
    health, hcode = _get_json(health_url, token, timeout_s=15.0)
    print(json.dumps(health, indent=2, ensure_ascii=False)[:4000])
    if hcode >= 500:
        print(f"FAIL health HTTP {hcode}")
        return 1

    plan_body: dict = {
        "instruction": args.instruction,
        "logical_camera_device": 0,
        "image_url": "embedded://verify/test",
    }
    if args.dual_jpeg:
        w, f = Path(args.dual_jpeg[0]), Path(args.dual_jpeg[1])
        plan_body["jpeg_base64"] = _load_jpeg_b64(w)
        plan_body["jpeg_base64_front"] = _load_jpeg_b64(f)
        print(f"POST /plan con JPEG inline: wrist={w.name} front={f.name}")
    else:
        print("POST /plan senza JPEG (worker stub potrebbe fallire se richiede fetch URL)")

    plan_url = base + "/plan"
    plan, pcode = _post_json(plan_url, plan_body, token, timeout_s=args.timeout)
    print(json.dumps(plan, indent=2, ensure_ascii=False)[:6000])
    if pcode >= 400 or not plan.get("ok"):
        print(f"FAIL plan HTTP {pcode}")
        return 1

    print("VERIFY_AWS_VLA_WORKER_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

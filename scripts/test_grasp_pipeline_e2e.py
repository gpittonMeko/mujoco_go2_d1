#!/usr/bin/env python3
"""Test E2E: worker AWS /plan + opzionale proxy NX grasp/health."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _get(url: str, token: str | None, timeout: float = 15) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Worker-Token"] = token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post(url: str, body: dict, token: str | None, timeout: float = 120) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["X-Worker-Token"] = token
    data = json.dumps(body).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method="POST"), timeout=timeout) as r:
        return json.loads(r.read().decode())


def _synthetic_jpeg_b64() -> str:
    import cv2
    import numpy as np

    img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.rectangle(img, (200, 150), (440, 330), (80, 200, 255), -1)
    _, jpg = cv2.imencode(".jpg", img)
    return base64.standard_b64encode(jpg.tobytes()).decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", default=os.environ.get("GO2_ANYGRASP_WORKER_URL") or "http://13.60.243.28:8765")
    ap.add_argument("--token", default=os.environ.get("GO2_WORKER_TOKEN") or "")
    ap.add_argument("--nx", default=os.environ.get("GO2_NX_HOST") or "192.168.123.18")
    ap.add_argument("--skip-nx", action="store_true")
    args = ap.parse_args()
    worker = args.worker.rstrip("/")
    token = (args.token or "").strip() or None
    if not token:
        pf = REPO / "go2-vla-pairing.env"
        if pf.is_file():
            for line in pf.read_text(encoding="utf-8").splitlines():
                if line.startswith("GO2_WORKER_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break

    print("1) Worker health")
    h = _get(worker + "/health", token)
    print(json.dumps({k: h.get(k) for k in ("ok", "backend", "backend_configured", "planner_import_ok", "graspgen_status")}, indent=2))
    if not h.get("ok"):
        return 1

    print("2) Worker plan (synthetic JPEG)")
    plan = _post(
        worker + "/plan",
        {"instruction": "pick box", "logical_camera_device": 0, "jpeg_base64": _synthetic_jpeg_b64(), "image_url": "embedded://e2e"},
        token,
    )
    if not plan.get("ok") or plan.get("backend") == "stub":
        print(json.dumps(plan, indent=2)[:3000])
        return 1
    print(
        "OK plan backend=%s preview_ok=%s xyz=%s"
        % (plan.get("backend"), (plan.get("preview") or {}).get("ok"), plan.get("grasp_display_base_link_m"))
    )

    if not args.skip_nx:
        print("3) NX grasp/health (proxy)")
        try:
            nh = _get(f"http://{args.nx}:5052/api/grasp/health", None, timeout=8)
            print(json.dumps(nh, indent=2, ensure_ascii=False)[:2500])
            if nh.get("worker_reachable"):
                print("NX proxy OK")
            else:
                print("WARN: NX up ma worker non raggiungibile dalla Jetson")
        except (urllib.error.URLError, OSError) as exc:
            print(f"SKIP NX (non raggiungibile): {exc}")

    print("TEST_GRASP_PIPELINE_E2E_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

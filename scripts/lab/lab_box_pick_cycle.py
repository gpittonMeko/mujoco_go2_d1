#!/usr/bin/env python3
"""Ciclo laboratorio: stand → box detect (YOLO/fallback) → crouch → START braccio → OpenVLA plan → movimento.

Chiama ``POST /api/mission/box_pick_cycle`` sulla dashboard operator sulla NX.

Esempio (dal PC sulla LAN):

  python scripts/lab/lab_box_pick_cycle.py --base http://192.168.123.18:5052

Requisiti sulla NX: ``GO2_ENABLE_BASE_MOTION=1``, braccio abilitato, worker OpenVLA raggiungibile
(``GO2_ANYGRASP_WORKER_URL``), ``data/start_alignment.json`` valido per START fluido.

Opzionale: ``GO2_DASHBOARD_PUBLIC_BASE`` sulla NX = stesso URL usato dal worker per scaricare il JPEG
(se non lo passi nel JSON, prova ``request.url_root`` lato server).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--base",
        default="http://192.168.123.18:5052",
        help="URL radice dashboard (es. http://IP_NX:5052)",
    )
    p.add_argument(
        "--prefix",
        default="",
        help="Prefisso mount Flask se usato (GO2_DASHBOARD_URL_PREFIX)",
    )
    p.add_argument(
        "--public-base",
        default="",
        help="URL pubblico per image_url (worker RTX); default = --base",
    )
    p.add_argument("--camera", type=int, default=6, choices=(0, 6), help="logical_camera_device / JPEG")
    p.add_argument("--instruction", default="pick up the white box", help="Istruzione OpenVLA (EN)")
    p.add_argument(
        "--execute-mode",
        default="openvla_then_ik",
        choices=("openvla_then_ik", "openvla_joints", "ik"),
        help="openvla_then_ik: prova giunti OpenVLA poi IK",
    )
    p.add_argument("--require-box", action="store_true", help="Fallisce se box detect non trova nulla")
    p.add_argument("--timeout", type=float, default=180.0, help="Timeout HTTP totale (s)")
    args = p.parse_args()

    base = args.base.rstrip("/")
    prefix = args.prefix.strip().rstrip("/")
    public_base = (args.public_base or base).rstrip("/")
    path = f"{prefix}/api/mission/box_pick_cycle" if prefix else "/api/mission/box_pick_cycle"
    url = base + path

    payload = {
        "confirm": "LAB_BOX_PICK_CYCLE",
        "dashboard_public_base": public_base,
        "logical_camera": int(args.camera),
        "instruction": str(args.instruction).strip(),
        "execute_mode": str(args.execute_mode),
        "require_box_detect": bool(args.require_box),
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = resp.getcode() or 200
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0 if out.get("ok") else 1
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}", file=sys.stderr)
        try:
            print(json.dumps(json.loads(err), indent=2, ensure_ascii=False))
        except json.JSONDecodeError:
            print(err[:4000])
        return 1
    except Exception as exc:
        print(repr(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

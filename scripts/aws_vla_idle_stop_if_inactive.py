#!/usr/bin/env python3
"""Ferma EC2 VLA se il worker non riceve POST /plan da N minuti (da PC con aws configure).

Uso:
  python scripts/aws_vla_idle_stop_if_inactive.py
  python scripts/aws_vla_idle_stop_if_inactive.py --idle-min 15 --dry-run

Legge/scrive ``data/go2_vla_last_plan_unix.txt`` (aggiornato dalla dashboard NX se configurato,
oppure tocca manualmente dopo ogni sessione). Se il file è vecchio, esegue ``aws_vla_ec2_control.py stop``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / "data" / "go2_vla_ec2_state.json"
STAMP = REPO / "data" / "go2_vla_last_plan_unix.txt"


def _worker_url() -> str | None:
    if not STATE.is_file():
        return None
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return (st.get("worker_url") or "").strip() or None


def _ec2_running(region: str) -> bool:
    if not STATE.is_file():
        return False
    st = json.loads(STATE.read_text(encoding="utf-8"))
    iid = st.get("instance_id")
    if not iid:
        return False
    env = os.environ.copy()
    env.setdefault("AWS_DEFAULT_REGION", region)
    env["AWS_CA_BUNDLE"] = ""
    cmd = ["aws", "--region", region, "--no-verify-ssl", "ec2", "describe-instances", "--instance-ids", iid,
           "--query", "Reservations[0].Instances[0].State.Name", "--output", "text"]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return r.returncode == 0 and (r.stdout or "").strip() == "running"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--idle-min", type=float, default=float(os.environ.get("GO2_VLA_IDLE_STOP_MIN", "25")))
    ap.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION") or "eu-north-1")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--touch", action="store_true", help="Segna attività adesso (dopo un plan riuscito)")
    args = ap.parse_args()

    if args.touch:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text(str(int(time.time())), encoding="utf-8")
        print("touched", STAMP)
        return 0

    if not _ec2_running(args.region):
        print("EC2 non running — nulla da fare")
        return 0

    now = time.time()
    if STAMP.is_file():
        try:
            last = float(STAMP.read_text(encoding="utf-8").strip())
        except ValueError:
            last = 0.0
    else:
        last = 0.0

    idle_s = now - last
    limit_s = args.idle_min * 60.0
    url = _worker_url()
    if url:
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=6) as resp:
                if resp.status == 200:
                    print("worker health OK", url)
        except (urllib.error.URLError, TimeoutError) as exc:
            print("worker unreachable:", exc)

    print(f"idle {idle_s:.0f}s limit {limit_s:.0f}s last_plan={last}")
    if idle_s < limit_s:
        print("still active — no stop")
        return 0

    if args.dry_run:
        print("dry-run: would stop EC2")
        return 0

    print("stopping EC2 (idle timeout)")
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "aws_vla_ec2_control.py"), "stop", "--region", args.region],
        cwd=str(REPO),
    )
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())

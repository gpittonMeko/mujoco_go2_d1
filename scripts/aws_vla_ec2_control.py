#!/usr/bin/env python3
"""Start / stop / status EC2 VLA worker (g5) — da PC o dalla NX con credenziali AWS.

Uso (PC):
  python scripts/aws_vla_ec2_control.py status
  python scripts/aws_vla_ec2_control.py stop
  python scripts/aws_vla_ec2_control.py start
  python scripts/aws_vla_ec2_control.py start --wait-health

Legge instance id da data/go2_vla_ec2_state.json (creato da provision-ec2.ps1).

Env AWS standard: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
(o profilo ~/.aws/credentials).

Sulla NX (dopo pair --install-ec2-control): stesso script, stesse env in nx_secrets_dashboard.sh
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

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "data" / "go2_vla_ec2_state.json"


def _load_state() -> dict:
    if not STATE_PATH.is_file():
        print(f"State non trovato: {STATE_PATH} — lancia prima provision-ec2.ps1", file=sys.stderr)
        sys.exit(2)
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def _aws_no_verify_ssl() -> bool:
    return os.environ.get("GO2_AWS_NO_VERIFY_SSL", "1").lower() in {"1", "true", "yes", "on"}


def _aws_cmd(args: list[str], region: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("AWS_DEFAULT_REGION", region)
    if _aws_no_verify_ssl():
        env["AWS_CA_BUNDLE"] = ""
    cmd = ["aws", "--region", region]
    if _aws_no_verify_ssl():
        cmd.append("--no-verify-ssl")
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def _public_ip(state: dict, region: str) -> str | None:
    iid = state.get("instance_id")
    if not iid:
        return None
    r = _aws_cmd(
        ["ec2", "describe-instances", "--instance-ids", iid, "--query", "Reservations[0].Instances[0].PublicIpAddress", "--output", "text"],
        region,
    )
    if r.returncode != 0:
        return state.get("public_ip")
    ip = (r.stdout or "").strip()
    return ip if ip and ip != "None" else None


def cmd_status(state: dict, region: str) -> int:
    iid = state["instance_id"]
    r = _aws_cmd(
        [
            "ec2",
            "describe-instances",
            "--instance-ids",
            iid,
            "--query",
            "Reservations[0].Instances[0].{State:State.Name,Ip:PublicIpAddress,Type:InstanceType}",
            "--output",
            "json",
        ],
        region,
    )
    if r.returncode != 0:
        print(r.stderr or r.stdout, file=sys.stderr)
        return r.returncode
    info = json.loads(r.stdout or "{}")
    worker_url = state.get("worker_url") or ""
    ip = info.get("Ip") or _public_ip(state, region)
    if ip:
        worker_url = f"http://{ip}:8765"
    print(json.dumps({"instance_id": iid, "ec2": info, "worker_url": worker_url, "pairing_file": state.get("pairing_file")}, indent=2))
    if ip and info.get("State") == "running":
        try:
            with urllib.request.urlopen(f"http://{ip}:8765/health", timeout=5) as resp:
                print("worker_health:", resp.read().decode()[:500])
        except (urllib.error.URLError, TimeoutError) as exc:
            print("worker_health: unreachable", exc)
    return 0


def cmd_start(state: dict, region: str, wait_health: bool) -> int:
    iid = state["instance_id"]
    r = _aws_cmd(["ec2", "start-instances", "--instance-ids", iid], region)
    if r.returncode != 0:
        print(r.stderr or r.stdout, file=sys.stderr)
        return r.returncode
    print("starting", iid)
    _aws_cmd(["ec2", "wait", "instance-running", "--instance-ids", iid], region)
    ip = _public_ip(state, region)
    if ip:
        state["public_ip"] = ip
        state["worker_url"] = f"http://{ip}:8765"
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
        print("public_ip", ip)
    if wait_health and ip:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://{ip}:8765/health", timeout=5):
                    print("worker health OK")
                    return 0
            except (urllib.error.URLError, TimeoutError):
                time.sleep(5)
        print("AVVISO: EC2 running ma worker :8765 non risponde ancora", file=sys.stderr)
    return 0


def cmd_stop(state: dict, region: str) -> int:
    iid = state["instance_id"]
    r = _aws_cmd(["ec2", "stop-instances", "--instance-ids", iid], region)
    if r.returncode != 0:
        print(r.stderr or r.stdout, file=sys.stderr)
        return r.returncode
    print("stopping", iid)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Control EC2 VLA worker")
    ap.add_argument("action", choices=["status", "start", "stop"])
    ap.add_argument("--wait-health", action="store_true")
    ap.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION") or "eu-north-1")
    args = ap.parse_args()

    state = _load_state()
    region = args.region or state.get("region") or "eu-west-1"

    if args.action == "status":
        return cmd_status(state, region)
    if args.action == "start":
        return cmd_start(state, region, args.wait_health)
    return cmd_stop(state, region)


if __name__ == "__main__":
    raise SystemExit(main())

"""Start/stop EC2 VLA worker dalla dashboard (subprocess verso scripts/aws_vla_ec2_control.py)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT


def _state_path() -> Path:
    return PROJECT_ROOT / "data" / "go2_vla_ec2_state.json"


def _control_script() -> Path:
    return PROJECT_ROOT / "scripts" / "aws_vla_ec2_control.py"


def ec2_control_available() -> bool:
    return _state_path().is_file() and _control_script().is_file()


def run_ec2_action(action: str, *, wait_health: bool = False, region: str | None = None) -> dict[str, Any]:
    if not ec2_control_available():
        return {
            "ok": False,
            "reason": "ec2_state_missing",
            "hint_it": "Manca data/go2_vla_ec2_state.json — lancia provision EC2 dal PC.",
        }
    cmd = [sys.executable, str(_control_script()), action]
    if wait_health and action == "start":
        cmd.append("--wait-health")
    reg = (region or os.environ.get("AWS_DEFAULT_REGION") or "eu-north-1").strip()
    if reg:
        cmd.extend(["--region", reg])
    env = os.environ.copy()
    env.setdefault("AWS_DEFAULT_REGION", reg)
    if os.environ.get("GO2_AWS_NO_VERIFY_SSL", "1").lower() in {"1", "true", "yes", "on"}:
        env["AWS_CA_BUNDLE"] = ""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=str(PROJECT_ROOT), env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "ec2_control_timeout", "action": action}
    out = (r.stdout or "") + (r.stderr or "")
    payload: dict[str, Any] = {
        "ok": r.returncode == 0,
        "action": action,
        "exit_code": r.returncode,
        "output_tail": out[-4000:] if out else "",
    }
    if r.returncode == 0 and action == "status" and _state_path().is_file():
        try:
            payload["state"] = json.loads(_state_path().read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    if not payload["ok"]:
        payload["hint_it"] = "Verifica credenziali AWS su NX (nx_secrets_dashboard.sh) o lancia dal PC."
    return payload

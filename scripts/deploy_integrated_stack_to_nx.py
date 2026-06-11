#!/usr/bin/env python3
"""Deploy stack integrato sulla Jetson: operator 5052 + D1 jog 5053 (+ Hermes 5054 opzionale).

Uso (PC sulla LAN Unitree):
  python scripts/deploy_integrated_stack_to_nx.py
  python scripts/deploy_integrated_stack_to_nx.py --hermes

Non modifica i rami originali; usa gli script di deploy esistenti in sequenza.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(script: str, extra: list[str] | None = None) -> int:
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / script), *(extra or [])]
    print(f"\n[integrated] >>> {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(REPO_ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="Deploy stack integrato go2-dash-integrated sulla NX")
    ap.add_argument(
        "--hermes",
        action="store_true",
        help="Dopo 5052+5053, deploy anche Hermes standalone su 5054",
    )
    ap.add_argument(
        "--skip-d1-jog",
        action="store_true",
        help="Solo dashboard operator 5052",
    )
    args = ap.parse_args()

    rc = _run("deploy_dashboard_to_nx.py")
    if rc != 0:
        return rc
    if not args.skip_d1_jog:
        rc = _run("deploy_d1_jog_to_nx.py")
        if rc != 0:
            return rc
        # deploy_d1_jog_to_nx.py ferma la dashboard 5052: riavviala per stack integrato.
        rc = _run("deploy_dashboard_to_nx.py")
        if rc != 0:
            return rc
    if args.hermes:
        rc = _run("deploy_hermes_to_nx.py")
        if rc != 0:
            return rc
    print("\n[integrated] OK — stack deploy completato.")
    print("  Operator: http://192.168.123.18:5052/operators")
    if not args.skip_d1_jog:
        print("  D1 jog:   http://192.168.123.18:5053/")
    if args.hermes:
        print("  Hermes:   http://192.168.123.18:5054/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

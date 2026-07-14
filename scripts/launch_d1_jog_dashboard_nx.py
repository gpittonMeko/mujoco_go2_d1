#!/usr/bin/env python3
"""Deploy + avvio dashboard jog D1 sulla NX.

Safety: questo script puo' riavviare i processi dashboard/hold. Non usarlo con
braccio sospeso, se non dopo conferma esplicita.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy e riavvio D1 jog dashboard su NX")
    parser.add_argument(
        "--confirm-restart-risk",
        action="store_true",
        help="Conferma esplicita: accetto il rischio di restart/handoff (possibile drop momentaneo se il braccio non e' sostenuto).",
    )
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Solo stato remoto; non fa deploy o restart.",
    )
    args = parser.parse_args()

    host = __import__("os").environ.get("GO2_NX_HOST", "192.168.123.18")
    port = __import__("os").environ.get("D1_JOG_PORT", "5056")

    if args.status_only:
        cmd = ["curl", "-s", f"http://{host}:5056/api/health"]
        rc = subprocess.call(cmd)
        if rc != 0:
            raise SystemExit(rc)
        return

    if not args.confirm_restart_risk:
        print("BLOCCATO per sicurezza: questo comando puo' riavviare handoff/daemon.")
        print("Usa --confirm-restart-risk solo con braccio sostenuto o in posa sicura a terra.")
        print("Per sola diagnostica usa: --status-only")
        raise SystemExit(2)

    script = ROOT / "scripts" / "deploy_d1_jog_to_nx.py"
    rc = subprocess.call([sys.executable, str(script)], cwd=str(ROOT))
    if rc != 0:
        raise SystemExit(rc)
    print(f"\nDashboard jog: http://{host}:{port}/")


if __name__ == "__main__":
    main()

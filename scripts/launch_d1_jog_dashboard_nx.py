#!/usr/bin/env python3
"""Deploy + avvio dashboard jog D1 sulla NX (porta 5053)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    script = ROOT / "scripts" / "deploy_d1_jog_to_nx.py"
    rc = subprocess.call([sys.executable, str(script)], cwd=str(ROOT))
    if rc != 0:
        raise SystemExit(rc)
    host = __import__("os").environ.get("GO2_NX_HOST", "192.168.123.18")
    port = __import__("os").environ.get("D1_JOG_PORT", "5053")
    print(f"\nDashboard jog: http://{host}:{port}/")


if __name__ == "__main__":
    main()

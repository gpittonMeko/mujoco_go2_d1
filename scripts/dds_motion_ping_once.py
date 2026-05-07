#!/usr/bin/env python3
"""Un colpo MotionSwitcher.CheckMode (DDS) — pensato per ``subprocess.run`` dalla dashboard."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from go2_accompany import dds_unitree_motion_ping


def main() -> None:
    iface = (os.environ.get("GO2_DDS_INTERFACE") or "").strip() or None
    domain = int(os.environ.get("GO2_DDS_DOMAIN", "0"))
    out = dds_unitree_motion_ping(project_root=ROOT, domain=domain, iface=iface)
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()

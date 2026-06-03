#!/usr/bin/env python3
"""Dashboard streaming camera Intel sulla NX — Vision Workspace (porta 5054)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("GO2_LOCAL", "1")

from go2_dashboard.vision import create_vision_app


def main() -> None:
    port = int(os.environ.get("VISION_PORT", "5054"))
    bind = os.environ.get("VISION_BIND", "0.0.0.0")
    app = create_vision_app()
    print(f"Vision dashboard http://{bind}:{port}/  (Intel logical {os.environ.get('VISION_CAMERA_LOGICAL', '6')})")
    app.run(host=bind, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()

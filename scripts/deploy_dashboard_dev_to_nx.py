#!/usr/bin/env python3
"""Deploy **istanza DEV** separata sulla NX — non tocca la dashboard del collega.

- Directory: ``/home/unitree/go2_visual_dashboard_dev``
- Porta: **5054** (il collega resta su **5052** / jog **5053**)
- Restart: solo ``fuser -k 5054`` + supervisore dev (NON ``pkill serve_dashboard_lite`` globale)
- Nessun cron @reboot (non sovrascrive l'autostart del collega)
- ``bin/`` → symlink al SDK già compilato in ``go2_visual_dashboard/bin``

Uso (dal PC in lab)::

    python scripts/deploy_dashboard_dev_to_nx.py

URL dashboard tua: ``http://192.168.123.18:5054``

**Attenzione hardware:** braccio/cane/camere sono condivisi — non comandare il robot da
entrambe le dashboard insieme. Coordinarsi col collega prima di muovere.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Config istanza DEV (prima di importare deploy_dashboard_to_nx).
os.environ.setdefault("GO2_DEPLOY_REMOTE_BASE", "/home/unitree/go2_visual_dashboard_dev")
os.environ.setdefault("GO2_DEPLOY_PORT", "5054")
os.environ.setdefault("GO2_DEPLOY_INSTANCE", "dev")
os.environ.setdefault("GO2_DEPLOY_SKIP_AUTOSTART", "1")
# Mesh già sulla NX nella tree principale — deploy dev più veloce.
os.environ.setdefault("GO2_DEPLOY_SKIP_MESHES", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_dashboard_to_nx import main  # noqa: E402

if __name__ == "__main__":
    main()

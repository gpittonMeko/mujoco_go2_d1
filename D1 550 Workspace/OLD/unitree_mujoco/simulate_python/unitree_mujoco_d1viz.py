#!/usr/bin/env python3
"""
Simulatore con scena **go2_d1_d1mesh** (mesh braccio D1), non il placeholder Z1 di `go2_d1.xml`.

Carica `config_d1viz.py` (eredita `config.py` ma imposta `ROBOT_SCENE` → `scene_d1_mesh.xml`)
ed esegue `unitree_mujoco.py` senza fork del codice viewer/thread.

Per Go2+Z1 standard usare `unitree_mujoco.py` + `config.py`.
"""

import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SCRIPTS = os.path.join(ROOT, "scripts")
for p in (HERE, ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import config_d1viz as _cfg  # noqa: E402
sys.modules["config"] = _cfg

runpy.run_path(os.path.join(HERE, "unitree_mujoco.py"), run_name="__main__")


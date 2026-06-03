#!/usr/bin/env python3
"""
Pipeline **solo** mesh D1 (`go2_d1_d1mesh.xml`): cinematica e pose in
`arm_kinematics_d1_template.py`. Sim: `unitree_mujoco_d1viz.py` + `config_d1viz.py`.

Il percorso Z1 resta in `run_go2_d1_ball.py` (nessuna logica D1 lì, solo questo
launcher inietta `go2_ball_d1_profile` + modulo `arm_kinematics` e riesegue lo
script con `runpy`).

Se non passi `--interface`, viene usato **lo** (loopback), come `config_d1viz.py`:
evita l'errore CycloneDDS *lan2: does not match an available interface* su PC senza
quella NIC. Per rete reale: `python3 run_go2_d1_ball_d1kin.py --interface <tua_if>`.
"""

import os
import runpy
import sys
import types


def _argv_has_interface(argv):
    for a in argv:
        if a == "--interface" or a.startswith("--interface="):
            return True
    return False


HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

if not _argv_has_interface(sys.argv):
    sys.argv.extend(["--interface", "lo"])

import arm_kinematics_d1_template as _d1kin  # noqa: E402

_prof = types.SimpleNamespace(
    ARM_FOLD=list(_d1kin.ARM_FOLD_POSE),
    ARM_REACH_FWD=list(_d1kin.ARM_REACH_FWD_POSE),
    SEARCH_POSES=list(_d1kin.SEARCH_POSES_D1),
    SYNC_ARM_FROM_LOWSTATE=False,
)
sys.modules["go2_ball_d1_profile"] = _prof
sys.modules["arm_kinematics"] = _d1kin

runpy.run_path(os.path.join(HERE, "run_go2_d1_ball.py"), run_name="__main__")

"""
Override di `config.py` per **scene_d1_mesh.xml** (Go2 + braccio Unitree D1, `go2_d1_d1mesh.xml`).

`config.py` + `scene.xml` restano per il modello **Z1** in `go2_d1.xml`.
"""

from config import *  # noqa: F401,F403

ARM_KINEMATICS_MODE = "d1"

ROBOT_SCENE = "../unitree_robots/go2_d1/scene_d1_mesh.xml"

# DDS su loopback per sim locale (config.py usa spesso "lan2" per rete reale).
INTERFACE = "lo"


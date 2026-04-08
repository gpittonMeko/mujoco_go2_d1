#!/usr/bin/env python3
"""
Wrapper copy for ball-grasp routine using D1 kinematics template,
without modifying the working run_go2_d1_ball.py.
"""

import os
import runpy
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Force script-level import `from arm_kinematics import ...`
# to resolve to D1 template in this experimental path.
import arm_kinematics_d1_template as _d1kin  # noqa: E402
sys.modules["arm_kinematics"] = _d1kin

runpy.run_path(os.path.join(HERE, "run_go2_d1_ball.py"), run_name="__main__")


#!/usr/bin/env python3
"""
Wrapper copy for D1 mesh visualization experiments.

Behavior:
- Loads config_d1viz (copy config)
- Runs original unitree_mujoco.py unchanged

This launcher intentionally avoids viewer monkey-patching to keep shutdown
and Qt threading behavior identical to the base simulator.
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


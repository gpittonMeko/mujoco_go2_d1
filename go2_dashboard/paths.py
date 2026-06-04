"""Percorsi repo e workspace D1 550 (condivisi tra monolite, deploy, operator)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- D1 550 Workspace ---------------------------------------------------------
D1_WORKSPACE = PROJECT_ROOT / "D1 550 Workspace"
D1_OLD = D1_WORKSPACE / "OLD"
D1_SDK = D1_WORKSPACE / "d1_sdk"
D1_SDK_SRC = D1_SDK / "d1_sdk"

D1_OLD_SCRIPTS = D1_OLD / "scripts"
D1_OLD_MSG = D1_OLD / "msg"
D1_OLD_DOCS = D1_OLD / "docs"
D1_OLD_GO2_DASHBOARD = D1_OLD / "go2_dashboard"
D1_OLD_UNITREE_MUJOCO = D1_OLD / "unitree_mujoco"

D1_550_DESCRIPTION = D1_OLD / "d1_550_description"
D1_GO2_D1 = D1_OLD_UNITREE_MUJOCO / "unitree_robots" / "go2_d1"
D1_GO2_D1_ASSETS = D1_GO2_D1 / "assets"
D1_GO2_D1_MESHES = D1_GO2_D1 / "d1_550_description" / "meshes"
D1_GO2_D1_URDF = D1_GO2_D1 / "d1_550_description" / "urdf" / "d1_550_description.urdf"
D1_GO2_D1_URDF_STANDALONE = D1_550_DESCRIPTION / "urdf" / "d1_550_description.urdf"
MUJOCO_SCENE_D1_MESH_XML = D1_GO2_D1 / "scene_d1_mesh.xml"
MUJOCO_GO2_D1_D1MESH_XML = D1_GO2_D1 / "go2_d1_d1mesh.xml"

D1_SDK_COMMAND_BIN = PROJECT_ROOT / "bin" / "d1_sdk_command"
D1_SDK_FEEDBACK_BIN = PROJECT_ROOT / "bin" / "d1_sdk_feedback"
D1_SDK_GET_ANGLES_BIN = PROJECT_ROOT / "bin" / "d1_sdk_get_angles"

D1_ARM_COMMAND_BIN = PROJECT_ROOT / "bin" / "d1_arm_command"
D1_ARM_FEEDBACK_BIN = PROJECT_ROOT / "bin" / "d1_arm_feedback_helper"
D1_BUILD_HELPERS_SH = D1_OLD_SCRIPTS / "build_d1_arm_helpers.sh"
D1_ARM_DDS_HELPER_CPP = D1_OLD_SCRIPTS / "d1_arm_dds_helper.cpp"
D1_ARM_FEEDBACK_HELPER_CPP = D1_OLD_SCRIPTS / "d1_arm_feedback_helper.cpp"
D1_ARM_SERVO_READ_PY = D1_OLD_SCRIPTS / "d1_arm_servo_read_python.py"
D1_DRAG_FOLLOW_PY = D1_OLD_SCRIPTS / "d1_drag_follow_experimental.py"
D1_ARM_KINEMATICS_PY = D1_OLD_SCRIPTS / "arm_kinematics_d1_template.py"
D1_SERVO_FEEDBACK_PY = D1_OLD_GO2_DASHBOARD / "d1_servo_feedback.py"
D1_PROTOCOL_DOC = D1_OLD_DOCS / "d1_arm_protocol_feasibility.md"

# Path relativi alla root repo (deploy / documentazione)
REL_GO2_D1 = "D1 550 Workspace/OLD/unitree_mujoco/unitree_robots/go2_d1"
REL_D1_OLD_SCRIPTS = "D1 550 Workspace/OLD/scripts"
REL_D1_BUILD_HELPERS = f"{REL_D1_OLD_SCRIPTS}/build_d1_arm_helpers.sh"


def ensure_d1_scripts_on_sys_path() -> None:
    """``arm_kinematics_d1_template`` (OLD) + ``box_grasp_planner`` (repo scripts/)."""
    for s in (str(D1_OLD_SCRIPTS), str(PROJECT_ROOT / "scripts")):
        if s not in sys.path:
            sys.path.insert(0, s)


def d1_urdf_search_paths() -> list[Path]:
    """URDF D1: env, standalone OLD, poi mesh tree go2_d1."""
    import os

    cands: list[Path] = []
    envp = os.environ.get("GO2_D1_URDF_PATH", "").strip()
    if envp:
        cands.append(Path(envp))
    cands.extend([D1_GO2_D1_URDF_STANDALONE, D1_GO2_D1_URDF])
    return [p for p in cands if str(p)]

"""Percorsi repo, SDK D1 ufficiale e fallback legacy (senza dipendere dal monolite)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- D1 550 Workspace (ramo d1-arm-control) — opzionale -----------------------
D1_WORKSPACE = PROJECT_ROOT / "D1 550 Workspace"
D1_OLD = D1_WORKSPACE / "OLD"
D1_SDK = D1_WORKSPACE / "d1_sdk"
D1_SDK_SRC = D1_SDK / "d1_sdk"

# Fallback: stesso layout del repo principale se il workspace non c'è
D1_OLD_SCRIPTS = D1_OLD / "scripts" if (D1_OLD / "scripts").is_dir() else PROJECT_ROOT / "scripts"
D1_OLD_GO2_DASHBOARD = (
    D1_OLD / "go2_dashboard" if (D1_OLD / "go2_dashboard").is_dir() else PROJECT_ROOT / "go2_dashboard"
)
D1_OLD_DOCS = D1_OLD / "docs" if (D1_OLD / "docs").is_dir() else PROJECT_ROOT / "docs"

# Binari DDS (SDK ufficiale preferito; legacy helper come fallback)
D1_SDK_COMMAND_BIN = PROJECT_ROOT / "bin" / "d1_sdk_command"
D1_SDK_FEEDBACK_BIN = PROJECT_ROOT / "bin" / "d1_sdk_feedback"
D1_SDK_GET_ANGLES_BIN = PROJECT_ROOT / "bin" / "d1_sdk_get_angles"

D1_ARM_COMMAND_BIN = PROJECT_ROOT / "bin" / "d1_arm_command"
D1_ARM_FEEDBACK_BIN = PROJECT_ROOT / "bin" / "d1_arm_feedback_helper"
D1_BUILD_HELPERS_SH = D1_OLD_SCRIPTS / "build_d1_arm_helpers.sh"
D1_ARM_DDS_HELPER_CPP = D1_OLD_SCRIPTS / "d1_arm_dds_helper.cpp"
D1_ARM_FEEDBACK_HELPER_CPP = D1_OLD_SCRIPTS / "d1_arm_feedback_helper.cpp"
D1_ARM_SERVO_READ_PY = D1_OLD_SCRIPTS / "d1_arm_servo_read_python.py"
D1_ARM_KINEMATICS_PY = D1_OLD_SCRIPTS / "arm_kinematics_d1_template.py"
D1_SERVO_FEEDBACK_PY = D1_OLD_GO2_DASHBOARD / "d1_servo_feedback.py"
D1_PROTOCOL_DOC = D1_OLD_DOCS / "d1_arm_protocol_feasibility.md"
if not D1_PROTOCOL_DOC.is_file():
    D1_PROTOCOL_DOC = PROJECT_ROOT / "docs" / "d1_arm_protocol_feasibility.md"

D1_BUILD_SDK_SH = PROJECT_ROOT / "scripts" / "build_d1_sdk.sh"

REL_D1_OLD_SCRIPTS = (
    "D1 550 Workspace/OLD/scripts"
    if (D1_OLD / "scripts").is_dir()
    else "scripts"
)


def sdk_binaries_ready() -> bool:
    return (
        D1_SDK_COMMAND_BIN.is_file()
        and os.access(D1_SDK_COMMAND_BIN, os.X_OK)
        and D1_SDK_FEEDBACK_BIN.is_file()
        and os.access(D1_SDK_FEEDBACK_BIN, os.X_OK)
    )


def prefer_sdk_backend() -> bool:
    """Backend movimento/feedback come dashboard jog (d1_sdk_*), se compilati."""
    if os.environ.get("D1_USE_SDK_BACKEND", "1").lower() in {"0", "false", "no", "off"}:
        return False
    return sdk_binaries_ready()


def ensure_d1_scripts_on_sys_path() -> None:
    """``arm_kinematics_d1_template`` + ``scripts/`` sulla sys.path."""
    for s in (str(D1_OLD_SCRIPTS), str(PROJECT_ROOT / "scripts")):
        if s not in sys.path:
            sys.path.insert(0, s)


def d1_urdf_search_paths() -> list[Path]:
    import os as _os

    cands: list[Path] = []
    envp = _os.environ.get("GO2_D1_URDF_PATH", "").strip()
    if envp:
        cands.append(Path(envp))
    for p in (
        D1_OLD / "d1_550_description" / "urdf" / "d1_550_description.urdf",
        PROJECT_ROOT / "unitree_mujoco" / "unitree_robots" / "go2_d1" / "d1_550_description" / "urdf" / "d1_550_description.urdf",
    ):
        if p.is_file():
            cands.append(p)
    return cands

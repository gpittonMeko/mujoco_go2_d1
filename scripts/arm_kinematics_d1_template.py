#!/usr/bin/env python3
"""
Cinematica / pose per il MJCF **go2_d1_d1mesh.xml** (Unitree D1, mesh d1_550).

**Non** sostituisce `arm_kinematics.py` (braccio Z1 in `go2_d1.xml`). Caricato solo da
`run_go2_d1_ball_d1kin.py` (alias `sys.modules["arm_kinematics"]`).

Limiti giunti allineati al datasheet D1 (arm_joint1..6 <-> giunti 0..5; pinza non e'
un DoF separato nel MJCF). La FK segue posizioni e assi dichiarati nel MJCF D1, evitando
la vecchia approssimazione planare Z1.

Riferimenti esterni (D1 / D1-T, URDF e servizi — verificare versione hardware):
  https://support.unitree.com/home/en/developer/D1Arm_services
  https://www.unitree.com/D1-T/
  URDF in pacchetto SDK Go2 (link segnalato in community):
  https://oss-global-cdn.unitree.com/static/9b20252a26374d50aa369532657d0143.zip
  Discussione: https://github.com/unitreerobotics/unitree_ros/issues/116
"""

from __future__ import annotations

import os

import numpy as np

# -- Parametri geometrici da go2_d1_d1mesh.xml ---------------------------------
ARM_BASE_X = 0.15
ARM_BASE_Y = 0.0
ARM_BASE_Z = 0.155  # riferimento legacy usato da run_go2_d1_ball per clamp target
ARM_MOUNT_Z = 0.06

TOOL_TIP_OFFSET = np.array([0.07, 0.0, 0.0], dtype=float)

# --- Camere MJCF ``go2_d1_d1mesh.xml`` (base_link + arm_link06) ----------------
# depth_camera: body base_link, pos="0.34 0 0.09" euler="0 -1.57 -1.57"
# arm_link00:   pos="0.15 0 0.06" → +X avanti nel frame braccio (origine arm_link00)
# Richiesta laboratorio: **19 cm** avanti rispetto all’origine arm_link00 lungo +X mount → 0.15+0.19 = 0.34 in base_link.
DEPTH_CAMERA_ARM_BASE_M = np.array([0.34 - 0.15, 0.0, 0.09 - 0.06], dtype=float)
# wrist_camera: parent arm_link06 — offset **locale** lungo il link (MJCF ``go2_d1_d1mesh.xml``).
# Più ``WRIST_CAMERA_ARM_BASE_EXTRA_M``: +Z nel frame arm_link00 («su» sul robot) così la camera resta
# sopra il polso anche quando il link è inclinato (non solo +Z locale link6).
WRIST_CAMERA_LOCAL_OFFSET_M = np.array([0.02, 0.0, 0.0], dtype=float)
WRIST_CAMERA_ARM_BASE_EXTRA_M = np.array([0.0, 0.0, 0.03], dtype=float)

# Ogni riga: body pos locale, asse hinge locale. Gli assi includono il segno MJCF
# (J1 ruota intorno a -Z, non +Z).
D1_CHAIN = [
    (np.array([0.0, 0.0, 0.0738], dtype=float), np.array([0.0, 0.0, -1.0], dtype=float)),
    (np.array([0.0, -0.0276, 0.0578], dtype=float), np.array([0.0, 1.0, 0.0], dtype=float)),
    (np.array([0.0, -0.0004, 0.27], dtype=float), np.array([0.0, 1.0, 0.0], dtype=float)),
    (np.array([0.05, 0.0275, 0.041325], dtype=float), np.array([1.0, 0.0, 0.0], dtype=float)),
    (np.array([0.15468, -0.0258, 0.0001], dtype=float), np.array([0.0, 1.0, 0.0], dtype=float)),
    (np.array([0.0777, 0.025822, -0.0010718], dtype=float), np.array([1.0, 0.0, 0.0], dtype=float)),
]

# Limiti (rad) = datasheet D1: J0/J3/J5 +/-135 deg, J1/J2/J4 +/-90 deg.
J_LIMITS = [
    (-2.35619, 2.35619),
    (-1.5708, 1.5708),
    (-1.5708, 1.5708),
    (-2.35619, 2.35619),
    (-1.5708, 1.5708),
    (-2.35619, 2.35619),
]

# Allineata al keyframe home in go2_d1_d1mesh.xml (ultimi 6 valori qpos braccio).
ARM_FOLD_POSE = [0.0, -1.5, 1.0, 0.22, 0.0, 0.0]

_b = ARM_FOLD_POSE
SEARCH_POSES_D1 = [
    list(_b),
    list(_b[:4]) + [1.0, 0.0],
    list(_b[:4]) + [1.0, 0.25],
    list(_b[:4]) + [1.0, 0.0],
    list(_b),
    list(_b[:4]) + [-1.0, 0.0],
    list(_b[:4]) + [-1.0, 0.25],
    list(_b[:4]) + [-1.0, 0.0],
    list(_b),
    [0.55, _b[1], _b[2], _b[3], 0.5, 0.0],
    [-0.55, _b[1], _b[2], _b[3], -0.5, 0.0],
    list(_b),
]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _clamp_workspace(x, y, z):
    """
    Safety envelope for real tests.
    Keeps targets in a compact frontal workspace.
    """
    x = clamp(x, 0.18, 0.72)
    y = clamp(y, -0.35, 0.35)
    z = clamp(z, 0.02, 0.50)
    return x, y, z


def _axis_angle(axis, angle):
    axis = np.asarray(axis, dtype=float)
    axis = axis / np.linalg.norm(axis)
    x, y, z = axis
    c = float(np.cos(angle))
    s = float(np.sin(angle))
    C = 1.0 - c
    return np.array([
        [c + x * x * C, x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, c + z * z * C],
    ], dtype=float)


def fk_full(q):
    """Restituisce posizione/orientamento di arm_link06 in base_link."""
    if len(q) != 6:
        raise ValueError("q deve contenere 6 giunti")
    pos = np.array([ARM_BASE_X, ARM_BASE_Y, ARM_MOUNT_Z], dtype=float)
    R = np.eye(3, dtype=float)
    for qi, (body_pos, axis) in zip(q, D1_CHAIN):
        pos = pos + R @ body_pos
        R = R @ _axis_angle(axis, qi)
    return pos, R


def fk_tool_tip(q):
    pos, R = fk_full(q)
    return pos + R @ TOOL_TIP_OFFSET


def _wrist_cam_local_offset(wrist_local_offset_m: np.ndarray | list | None) -> np.ndarray:
    off = np.asarray(WRIST_CAMERA_LOCAL_OFFSET_M, dtype=float)
    if wrist_local_offset_m is not None:
        off = off + np.asarray(wrist_local_offset_m, dtype=float).reshape(3)
    return off


def fk_wrist_camera_center_m(
    q: list | np.ndarray, wrist_local_offset_m: np.ndarray | list | None = None
) -> np.ndarray:
    """Centro ottico ``wrist_camera`` (MJCF) nel frame base braccio (origine arm_link00)."""
    qn = _clamp_q(np.asarray(q, dtype=float))
    pos, R = fk_full(qn)
    return (
        pos
        + R @ _wrist_cam_local_offset(wrist_local_offset_m)
        + WRIST_CAMERA_ARM_BASE_EXTRA_M
    )


def fk_wrist_camera_optical_axis_unit_m(q: list | np.ndarray) -> np.ndarray:
    """Asse ottico ``wrist_camera`` come MJCF: ``euler="0 -1.5708 -1.5708"``, vista lungo -Z camera."""
    qn = _clamp_q(np.asarray(q, dtype=float))
    _, R_link = fk_full(qn)
    ex, ey, ez = 0.0, -float(np.pi) / 2.0, -float(np.pi) / 2.0
    rz = _axis_angle(np.array([0.0, 0.0, 1.0], dtype=float), ez)
    ry = _axis_angle(np.array([0.0, 1.0, 0.0], dtype=float), ey)
    rx = _axis_angle(np.array([1.0, 0.0, 0.0], dtype=float), ex)
    r_cam = rz @ ry @ rx
    v_local = r_cam @ np.array([0.0, 0.0, -1.0], dtype=float)
    v_arm = R_link @ v_local
    n = float(np.linalg.norm(v_arm))
    if n < 1e-9:
        return np.array([1.0, 0.0, 0.0], dtype=float)
    return (v_arm / n).astype(float)


def fk_wrist_camera_view_axis_unit_m(
    q: list | np.ndarray, wrist_local_offset_m: np.ndarray | list | None = None
) -> np.ndarray:
    """Versore vista camera polso: asse ottico MJCF (traslazione slider non ruota il montaggio)."""
    _ = wrist_local_offset_m
    return fk_wrist_camera_optical_axis_unit_m(q)


def depth_camera_optical_axis_unit_arm_base() -> np.ndarray:
    """
    Versore asse ottico ``depth_camera`` nel frame base braccio (arm_link00 = FK).
    Usa gli stessi angoli ``euler`` del MJCF su base_link e convenzione MuJoCo (camera -Z).
    Composizione rotazioni: Rz(ez) @ Ry(ey) @ Rx(ex) con ex,ey,ez = euler del file.
    """
    ex, ey, ez = 0.0, -float(np.pi) / 2.0, -float(np.pi) / 2.0
    rz = _axis_angle(np.array([0.0, 0.0, 1.0], dtype=float), ez)
    ry = _axis_angle(np.array([0.0, 1.0, 0.0], dtype=float), ey)
    rx = _axis_angle(np.array([1.0, 0.0, 0.0], dtype=float), ex)
    r = rz @ ry @ rx
    v = r @ np.array([0.0, 0.0, -1.0], dtype=float)
    n = float(np.linalg.norm(v))
    if n < 1e-9:
        return np.array([0.0, 0.0, -1.0], dtype=float)
    return (v / n).astype(float)


def nominal_object_along_depth_optical_arm_m(depth_m: float | None = None) -> np.ndarray:
    """
    Punto nominale nel frame **arm_link00** (base braccio FK): centro ottico MJCF
    ``depth_camera`` (stesso di ``DEPTH_CAMERA_ARM_BASE_M``) + ``depth_m`` lungo
    l'asse ottico (MuJoCo ``-Z`` camera nel mondo, vedi ``depth_camera_optical_axis_unit_arm_base``).

    Distanza tipica laboratorio oggetto davanti al muso: ``GO2_OBJECT_NOMINAL_DEPTH_ALONG_OPTICAL_M``
    (default ``0.20`` m). Non sostituisce la triangolazione reale da depth/VIO.
    """
    d = (
        float(os.environ.get("GO2_OBJECT_NOMINAL_DEPTH_ALONG_OPTICAL_M", "0.20"))
        if depth_m is None
        else float(depth_m)
    )
    ax = depth_camera_optical_axis_unit_arm_base()
    return (DEPTH_CAMERA_ARM_BASE_M + d * ax).astype(float)


def _clamp_q(q):
    return np.array([clamp(float(q[i]), *J_LIMITS[i]) for i in range(6)], dtype=float)


def quat_wxyz_from_axis_angle(axis: np.ndarray | list, angle: float) -> list[float]:
    """Unit quaternion (w, x, y, z) for rotation by ``angle`` about ``axis`` (right-hand)."""
    ax = np.asarray(axis, dtype=float)
    n = float(np.linalg.norm(ax))
    if n < 1e-12:
        return [1.0, 0.0, 0.0, 0.0]
    ax = ax / n
    ha = float(angle) * 0.5
    s = np.sin(ha)
    return [
        float(np.cos(ha)),
        float(ax[0] * s),
        float(ax[1] * s),
        float(ax[2] * s),
    ]


def quat_wxyz_to_xyzw(q_wxyz: list[float]) -> list[float]:
    w, x, y, z = (float(q_wxyz[i]) for i in range(4))
    return [x, y, z, w]


def fk_d1_joint_locals_m(q: list | np.ndarray) -> list[dict[str, list[float]]]:
    """
    Trasformazioni **locali** tra link D1 consecutivi (MuJoCo ``go2_d1_d1mesh.xml``):
    per ogni giunto i: traslazione parent→child come ``D1_CHAIN[i]``, poi rotazione
    ``arm_joint(i+1)`` attorno ad ``axis``. Ordine allineato a ``fk_chain_positions``.
    """
    qn = _clamp_q(np.asarray(q, dtype=float))
    out: list[dict[str, list[float]]] = []
    for i in range(6):
        body_pos, axis = D1_CHAIN[i]
        qwxyz = quat_wxyz_from_axis_angle(axis, float(qn[i]))
        out.append(
            {
                "translation_m": [round(float(body_pos[j]), 6) for j in range(3)],
                "quaternion_wxyz": [round(float(qwxyz[j]), 6) for j in range(4)],
                "quaternion_xyzw": [round(float(x), 6) for x in quat_wxyz_to_xyzw(qwxyz)],
            }
        )
    return out


def fk_chain_positions(q: list | np.ndarray) -> list[list[float]]:
    """
    Punti 3D nel frame **base braccio** (metri): mount, giunti intermedi, punta utensile.
    Restituisce 8 punti: origine, dopo ogni link (6), tool tip.
    """
    if len(q) != 6:
        raise ValueError("q deve contenere 6 giunti (rad)")
    qn = _clamp_q(np.asarray(q, dtype=float))
    pos = np.array([ARM_BASE_X, ARM_BASE_Y, ARM_MOUNT_Z], dtype=float)
    R = np.eye(3, dtype=float)
    pts: list[np.ndarray] = [pos.copy()]
    for qi, (body_pos, axis) in zip(qn, D1_CHAIN):
        pos = pos + R @ body_pos
        pts.append(pos.copy())
        R = R @ _axis_angle(axis, float(qi))
    tip = pos + R @ TOOL_TIP_OFFSET
    pts.append(np.asarray(tip, dtype=float).reshape(3))
    return [[round(float(p[0]), 5), round(float(p[1]), 5), round(float(p[2]), 5)] for p in pts]


def _numeric_jacobian(q, tip, eps=1e-5):
    J = np.zeros((3, 6), dtype=float)
    for i in range(6):
        q2 = q.copy()
        q2[i] = clamp(q2[i] + eps, *J_LIMITS[i])
        if q2[i] == q[i]:
            q2[i] = clamp(q2[i] - eps, *J_LIMITS[i])
        denom = q2[i] - q[i]
        if abs(denom) < 1e-12:
            continue
        J[:, i] = (fk_tool_tip(q2) - tip) / denom
    return J


def ik_reach(target_x, target_y, target_z, *, primary_seed: list[float] | None = None):
    target_x, target_y, target_z = _clamp_workspace(target_x, target_y, target_z)
    target = np.array([target_x, target_y, target_z], dtype=float)

    seeds: list[list[float]] = []
    if primary_seed is not None and len(primary_seed) >= 6:
        seeds.append([float(primary_seed[i]) for i in range(6)])
    seeds.extend(
        [
            ARM_FOLD_POSE,
            [0.0, -1.2, 0.8, 0.0, 0.0, 0.0],
            [0.0, -0.8, 0.5, 0.0, 0.4, 0.0],
            [0.0, -0.4, 0.2, 0.0, 0.8, 0.0],
            [0.5, -1.0, 0.8, 0.0, 0.3, 0.0],
            [-0.5, -1.0, 0.8, 0.0, -0.3, 0.0],
        ]
    )

    best_q = None
    best_err = float("inf")
    damping = 2e-3
    identity = np.eye(3, dtype=float)
    for seed in seeds:
        q = _clamp_q(seed)
        for _ in range(180):
            tip = fk_tool_tip(q)
            err = target - tip
            err_norm = float(np.linalg.norm(err))
            if err_norm < 0.006:
                break
            J = _numeric_jacobian(q, tip)
            dq = J.T @ np.linalg.solve(J @ J.T + damping * identity, err)
            step_norm = float(np.linalg.norm(dq))
            if step_norm > 0.18:
                dq *= 0.18 / step_norm
            q = _clamp_q(q + dq)

        err_norm = float(np.linalg.norm(target - fk_tool_tip(q)))
        if err_norm < best_err:
            best_err = err_norm
            best_q = q.copy()

    if best_q is None or best_err > 0.025:
        return None

    return best_q.tolist()


def step_toward(current, target, max_step):
    out = []
    for c, t in zip(current, target):
        diff = t - c
        step = clamp(diff, -max_step, max_step)
        out.append(c + step)
    return out


def smooth(current, target, alpha=0.05):
    return [c + alpha * (t - c) for c, t in zip(current, target)]


_rfp = ik_reach(0.42, 0.0, 0.12)
ARM_REACH_FWD_POSE = (
    list(_rfp)
    if _rfp is not None
    else [0.0, -1.2, 0.85, 0.25, 0.0, -0.5]
)

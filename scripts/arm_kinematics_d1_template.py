#!/usr/bin/env python3
"""
Cinematica / pose per il MJCF **go2_d1_d1mesh.xml** (Unitree D1, mesh d1_550).

**Non** sostituisce `arm_kinematics.py` (braccio Z1 in `go2_d1.xml`). Caricato solo da
`run_go2_d1_ball_d1kin.py` (alias `sys.modules["arm_kinematics"]`).

Limiti giunti allineati al datasheet D1 (arm_joint1..6 ↔ giunti 0..5; pinza non è DoF MJCF).
"""

import math
import numpy as np

# ── Parametri geometrici D1-like (stima conservativa) ─────────────────────────
ARM_BASE_X = 0.15
ARM_BASE_Y = 0.0
ARM_BASE_Z = 0.155

L_UPPER = 0.30
L_FORE_X = 0.20
L_FORE_Z = 0.04
L_WRIST = 0.13

# Limiti (rad) = datasheet D1: J0 ±135°, J1–J2–J4 ±90°, J3–J5 ±135° → arm_joint1..6
J_LIMITS = [
    (-2.35619, 2.35619),
    (-1.5708, 1.5708),
    (-1.5708, 1.5708),
    (-2.35619, 2.35619),
    (-1.5708, 1.5708),
    (-2.35619, 2.35619),
]

# Allineate al keyframe home in go2_d1_d1mesh.xml (ultimi 6 valori qpos braccio).
ARM_FOLD_POSE = [0.0, -1.5, 1.0, 0.22, 0.0, 0.0]

# ARM_REACH_FWD_POSE: assegnata dopo ik_reach (stessi L_*, J_LIMITS) — braccio più avanti per wrist cam in WALK.

_b = ARM_FOLD_POSE
# Ricerca: fissi j2–j4 come home; variano j5/j6 e leggermente j1 per la wrist cam.
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


def rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def fk_planar(j2, j3, j4):
    s23 = j2 + j3
    s234 = s23 + j4
    x = (-L_UPPER * math.cos(j2)
         + L_FORE_X * math.cos(s23) + L_FORE_Z * math.sin(s23)
         + L_WRIST * math.cos(s234))
    z = (L_UPPER * math.sin(j2)
         - L_FORE_X * math.sin(s23) + L_FORE_Z * math.cos(s234)
         - L_WRIST * math.sin(s234))
    return x, z


def fk_full(q):
    j1, j2, j3, j4, j5, j6 = q[0], q[1], q[2], q[3], q[4], q[5]
    x_plane, z_plane = fk_planar(j2, j3, j4)
    c1, s1 = math.cos(j1), math.sin(j1)
    x_arm = x_plane * c1
    y_arm = x_plane * s1
    z_arm = z_plane
    pos_arm = np.array([x_arm, y_arm, z_arm])
    pos_base = np.array([ARM_BASE_X, ARM_BASE_Y, ARM_BASE_Z]) + pos_arm
    R = rot_z(j1) @ rot_y(j2 + j3 + j4) @ rot_z(j5) @ rot_x(j6)
    return pos_base, R


def fk_tool_tip(q):
    pos, R = fk_full(q)
    tool_fwd = R[:, 0]
    tip = pos + 0.07 * tool_fwd
    return tip


def ik_reach(target_x, target_y, target_z):
    target_x, target_y, target_z = _clamp_workspace(target_x, target_y, target_z)
    dx = target_x - ARM_BASE_X
    dy = target_y - ARM_BASE_Y
    dz = target_z - ARM_BASE_Z

    j1 = math.atan2(dy, max(math.sqrt(dx * dx + dy * dy), 0.01))
    j1 = clamp(j1, *J_LIMITS[0])

    r_t = math.sqrt(dx * dx + dy * dy)
    z_rel = target_z - ARM_BASE_Z

    best, best_err = None, 1e9
    for j2i in [0.2, 0.5, 0.9, 1.2, 1.5]:
        for j3i in [-1.2, -0.8, -0.4, 0.0, 0.4, 0.8, 1.2]:
            for j4i in [-1.2, -0.6, 0.0, 0.6, 1.2]:
                j2, j3, j4 = j2i, j3i, j4i
                lr = 0.6
                eps = 1e-5
                for _ in range(150):
                    x, z = fk_planar(j2, j3, j4)
                    ex, ez = x - r_t, z - z_rel
                    if ex * ex + ez * ez < 1e-6:
                        break
                    dx2 = (fk_planar(j2 + eps, j3, j4)[0] - x) / eps
                    dz2 = (fk_planar(j2 + eps, j3, j4)[1] - z) / eps
                    dx3 = (fk_planar(j2, j3 + eps, j4)[0] - x) / eps
                    dz3 = (fk_planar(j2, j3 + eps, j4)[1] - z) / eps
                    dx4 = (fk_planar(j2, j3, j4 + eps)[0] - x) / eps
                    dz4 = (fk_planar(j2, j3, j4 + eps)[1] - z) / eps
                    j2 = clamp(j2 - lr * (ex * dx2 + ez * dz2), *J_LIMITS[1])
                    j3 = clamp(j3 - lr * (ex * dx3 + ez * dz3), *J_LIMITS[2])
                    j4 = clamp(j4 - lr * (ex * dx4 + ez * dz4), *J_LIMITS[3])
                    lr *= 0.992

                x, z = fk_planar(j2, j3, j4)
                e = (x - r_t) ** 2 + (z - z_rel) ** 2
                if e < best_err:
                    best_err = e
                    best = (j2, j3, j4)

    if best is None or best_err > 0.015:
        return None

    j2, j3, j4 = best
    j2 = clamp(j2, J_LIMITS[1][0], J_LIMITS[1][1])
    j5 = 0.0
    j6 = -0.65
    return [j1, j2, j3, j4, j5, j6]


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


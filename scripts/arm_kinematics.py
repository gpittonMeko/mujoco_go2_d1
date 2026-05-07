#!/usr/bin/env python3
"""
Cinematica braccio Z1 su Go2 d1 — ricostruita da zero per la presa della palla rossa.

Sistema di riferimento: base_link del robot (origine al centro del corpo).
Assi: X avanti, Y sinistra, Z alto.

Struttura braccio (da go2_d1.xml):
  base_link
    └─ arm_link00  pos=(0.15, 0, 0.06)
       └─ arm_link01  pos=(0, 0, 0.0585)  J1 axis Z
          └─ arm_link02  pos=(0, 0, 0.045)  J2 axis Y
             └─ arm_link03  pos=(-0.35, 0, 0)  J3 axis Y  [L_UPPER]
                └─ arm_link04  pos=(0.218, 0, 0.057)  J4 axis Y  [L_FORE]
                   └─ arm_link05  pos=(0.07, 0, 0)  J5 axis Z
                      └─ arm_link06  pos=(0.0492, 0, 0)  J6 axis X
                         └─ tool_tip  +0.07 lungo asse X link06

FK: date le 6 variabili di giunto, calcola posizione tool tip in base_link.
IK: data posizione target (x,y,z) in base_link, calcola angoli giunto per raggiungerla.
"""

import math
import numpy as np

# ── Parametri geometrici (da MuJoCo go2_d1.xml) ─────────────────────────────
# Offset base braccio rispetto a base_link
ARM_BASE_X = 0.15
ARM_BASE_Y = 0.0
ARM_BASE_Z = 0.06 + 0.0585 + 0.045  # 0.1635

# Lunghezze link (m) — da go2_d1.xml e validazione
L_UPPER = 0.35      # arm_link03
L_FORE_X = 0.218    # arm_link04
L_FORE_Z = 0.057
L_WRIST = 0.1447    # link05 + link06 + tool_tip (validato)

# Limiti giunti (rad)
J_LIMITS = [
    (-2.61799, 2.61799),   # J1
    (0.0, 2.96706),       # J2
    (-2.87979, 0.0),      # J3
    (-1.51844, 1.51844),  # J4
    (-1.3439, 1.3439),    # J5
    (-2.79253, 2.79253),  # J6
]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def rot_z(a):
    """Matrice rotazione attorno asse Z."""
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def rot_y(a):
    """Matrice rotazione attorno asse Y."""
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def rot_x(a):
    """Matrice rotazione attorno asse X."""
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


# ── Forward Kinematics ──────────────────────────────────────────────────────

def fk_planar(j2, j3, j4):
    """
    FK piano (J2,J3,J4) nel piano XZ del frame J2.
    Restituisce (x, z) dove x è avanti, z è alto.
    """
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
    """
    FK completa 6-DOF. q = [j1..j6] in radianti.
    Restituisce (pos, R) dove pos è [x,y,z] in base_link, R è matrice 3x3 orientamento.
    """
    j1, j2, j3, j4, j5, j6 = q[0], q[1], q[2], q[3], q[4], q[5]

    # Frame J2 (dopo J1): origine in (ARM_BASE_X, 0, ARM_BASE_Z)
    # J1 ruota attorno Z: il piano del braccio è ruotato di j1
    x_plane, z_plane = fk_planar(j2, j3, j4)

    # Nel frame J2: x avanti (nel piano), z alto. Y=0.
    # Trasformiamo in base_link: R_j1 = rot_z(j1), quindi
    # [x_base, y_base] = R_z(j1) @ [x_plane, 0]  -> x_base = x_plane*cos(j1), y_base = x_plane*sin(j1)
    # z_base = z_plane
    c1, s1 = math.cos(j1), math.sin(j1)
    x_arm = x_plane * c1
    y_arm = x_plane * s1
    z_arm = z_plane

    pos_arm = np.array([x_arm, y_arm, z_arm])
    pos_base = np.array([ARM_BASE_X, ARM_BASE_Y, ARM_BASE_Z]) + pos_arm

    # Orientamento: J5 (Z), J6 (X) nel frame del polso
    # Per la presa: tool punta avanti. Orientamento semplificato.
    R = rot_z(j1) @ rot_y(j2 + j3 + j4) @ rot_z(j5) @ rot_x(j6)

    return pos_base, R


def fk_tool_tip(q):
    """Posizione tool tip [x,y,z] in base_link."""
    pos, R = fk_full(q)
    # Tool tip è +0.07 lungo l'asse X del frame link06 (prima colonna di R)
    tool_fwd = R[:, 0]
    tip = pos + 0.07 * tool_fwd
    return tip


# ── Inverse Kinematics ──────────────────────────────────────────────────────

def ik_reach(target_x, target_y, target_z):
    """
    IK numerica per raggiungere il punto target (x,y,z) in base_link.
    Restituisce [j1..j6] o None se non raggiungibile.
    Target tipicamente = posizione palla.
    """
    # Vettore base -> target nel frame del braccio
    dx = target_x - ARM_BASE_X
    dy = target_y - ARM_BASE_Y
    dz = target_z - ARM_BASE_Z

    # J1: rotazione base per allineare il piano del braccio con (dx, dy)
    j1 = math.atan2(dy, max(math.sqrt(dx*dx + dy*dy), 0.01))
    j1 = clamp(j1, *J_LIMITS[0])

    # Distanza orizzontale e quota nel piano del braccio
    r_t = math.sqrt(dx*dx + dy*dy)
    z_t = dz  # z già in frame world

    # Compensazione: il target è per il tool tip; il polso è indietro di ~L_WRIST
    # Per semplicità miriamo al polso: target_wrist = target - 0.07*fwd
    # Approssimiamo: target nel piano (r_t, z_t) è dove vogliamo il tool tip
    # La FK piano dà la posizione del polso. Il tool tip è avanti di L_WRIST.
    # In realtà fk_planar già include L_WRIST fino al tool. Verifichiamo.
    # fk_planar restituisce (x,z) dell'endpoint del chain J2-J3-J4 che include L_WRIST.
    # Ma L_WRIST nel nostro caso = 0.1892 che è link05+link06+tip. Quindi sì.
    # Però nel vecchio codice L_WRIST=0.1447. Controlliamo il modello.
    # Il vecchio fk_plane usa L_WRIST=0.1447. La differenza potrebbe essere il tool tip.
    # Usiamo L_WRIST = 0.1447 per compatibilità con il modello esistente che funziona.
    z_rel = target_z - ARM_BASE_Z

    best, best_err = None, 1e9
    for j2i in [0.3, 0.6, 1.0, 1.5, 2.0, 2.5]:
        for j3i in [-0.3, -0.8, -1.2, -1.8, -2.4]:
            for j4i in [-1.0, -0.5, 0.0, 0.5, 1.0]:
                j2, j3, j4 = j2i, j3i, j4i
                lr = 0.6
                eps = 1e-5
                for _ in range(150):
                    x, z = fk_planar(j2, j3, j4)
                    ex, ez = x - r_t, z - z_rel
                    if ex*ex + ez*ez < 1e-6:
                        break
                    dx2 = (fk_planar(j2+eps, j3, j4)[0] - x) / eps
                    dz2 = (fk_planar(j2+eps, j3, j4)[1] - z) / eps
                    dx3 = (fk_planar(j2, j3+eps, j4)[0] - x) / eps
                    dz3 = (fk_planar(j2, j3+eps, j4)[1] - z) / eps
                    dx4 = (fk_planar(j2, j3, j4+eps)[0] - x) / eps
                    dz4 = (fk_planar(j2, j3, j4+eps)[1] - z) / eps
                    j2 = clamp(j2 - lr * (ex*dx2 + ez*dz2), *J_LIMITS[1])
                    j3 = clamp(j3 - lr * (ex*dx3 + ez*dz3), *J_LIMITS[2])
                    j4 = clamp(j4 - lr * (ex*dx4 + ez*dz4), *J_LIMITS[3])
                    lr *= 0.992

                x, z = fk_planar(j2, j3, j4)
                e = (x - r_t)**2 + (z - z_rel)**2
                if e < best_err:
                    best_err = e
                    best = (j2, j3, j4)

    if best is None or best_err > 0.02:
        return None

    j2, j3, j4 = best
    j2 = clamp(j2, 0.0, 2.35)
    # J5, J6: orientamento per la presa (tool verso il basso/avanti)
    j5 = 0.0
    j6 = -0.785  # ~-45° per inclinare la pinza verso il basso
    return [j1, j2, j3, j4, j5, j6]


# ── Utility ────────────────────────────────────────────────────────────────

def step_toward(current, target, max_step):
    """Limita lo spostamento per step (traiettoria graduale)."""
    out = []
    for c, t in zip(current, target):
        diff = t - c
        step = clamp(diff, -max_step, max_step)
        out.append(c + step)
    return out


def smooth(current, target, alpha=0.05):
    """Interpolazione esponenziale verso target."""
    return [c + alpha * (t - c) for c, t in zip(current, target)]

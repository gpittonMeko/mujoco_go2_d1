#!/usr/bin/env python3
"""
Policy open source di camminata: trot gait semplificato per Go2.

Trot = andatura a diagonale: FR+RL e FL+RR si muovono in coppia.
Usa oscillazioni sinusoidali sulle articolazioni per generare il passo.

Uso:
  1. Avvia il simulatore: cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py
  2. Esegui: python3 scripts/trot_gait.py [vx] [vy] [vyaw] [--arm-hold]
     - vx, vy, vyaw: velocità desiderate (default 0.2, 0, 0)
     - --arm-hold: per go2_d1, tiene il braccio ripiegato
     - Ctrl+C per fermare
"""

import time
import sys
import os
import math
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_sdk2_python"))

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelFactoryInitialize
from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_
from unitree_sdk2py.utils.crc import CRC

# Pose base in piedi (12 motori: FR, FL, RR, RL)
STAND_POSE = np.array([
    0.006, 0.61, -1.22, -0.006, 0.61, -1.22,
    0.006, 0.61, -1.22, -0.006, 0.61, -1.22
], dtype=float)

# go2_d1: compensa peso braccio (CoM avanti) → anteriori piegate, posteriori ben estese
STAND_POSE_ARM = np.array([
    0.0, 0.74, -1.42,  0.0, 0.74, -1.42,   # FR, FL: anteriori più piegate
    0.0, 1.08, -1.82,  0.0, 1.08, -1.82    # RR, RL: posteriori molto estese
], dtype=float)

DT = 0.002
KP = 40.0
KD = 3.0
FREQ = 1.2  # Hz - frequenza del trot
ARM_HOLD = [0.0, 0.2, -0.4, -0.2, 0.0, 0.0]  # go2_d1: braccio chiuso (tau=q_des, kp=kd=0)
SWING_AMP = 0.15   # ampiezza oscillazione thigh (rad)
STANCE_AMP = 0.08  # ampiezza oscillazione calf (rad)
# go2_d1: oscillazioni ridotte per stabilità con braccio
SWING_AMP_ARM = 0.10
STANCE_AMP_ARM = 0.05


def init_cmd():
    cmd = unitree_go_msg_dds__LowCmd_()
    cmd.head[0] = 0xFE
    cmd.head[1] = 0xEF
    cmd.level_flag = 0xFF
    cmd.gpio = 0
    for i in range(20):
        cmd.motor_cmd[i].mode = 0x01
        cmd.motor_cmd[i].q = 0.0
        cmd.motor_cmd[i].kp = 0.0
        cmd.motor_cmd[i].dq = 0.0
        cmd.motor_cmd[i].kd = 0.0
        cmd.motor_cmd[i].tau = 0.0
    return cmd


def trot_pose(t, vx, vy, vyaw, arm_hold=False):
    """
    Calcola posizioni articolari per trot.
    FR(0-2), FL(3-5), RR(6-8), RL(9-11)
    Trot: FR+RL in fase, FL+RR in fase opposta.
    arm_hold: usa pose compensata e oscillazioni ridotte per go2_d1.
    """
    phase = 2 * math.pi * FREQ * t
    base = STAND_POSE_ARM if arm_hold else STAND_POSE
    swing = SWING_AMP_ARM if arm_hold else SWING_AMP
    stance = STANCE_AMP_ARM if arm_hold else STANCE_AMP
    q = base.copy()

    # Coppie diagonali: FR+RL (phase), FL+RR (phase+pi)
    for leg_offset, phase_off in [(0, 0), (3, math.pi), (6, math.pi), (9, 0)]:
        p = phase + phase_off
        q[leg_offset + 1] += swing * math.sin(p)
        q[leg_offset + 2] += stance * math.sin(p)

    return q


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Trot gait per Go2")
    parser.add_argument("vx", type=float, nargs="?", default=0.2)
    parser.add_argument("vy", type=float, nargs="?", default=0.0)
    parser.add_argument("vyaw", type=float, nargs="?", default=0.0)
    parser.add_argument("--arm-hold", action="store_true", help="go2_d1: braccio ripiegato")
    parser.add_argument("--interface", type=str, default=None)
    args = parser.parse_args()
    vx, vy, vyaw = args.vx, args.vy, args.vyaw
    arm_hold = args.arm_hold

    iface = args.interface
    if iface is None:
        try:
            sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python"))
            import config as sim_config
            iface = sim_config.INTERFACE
        except ImportError:
            iface = "lo"
    ChannelFactoryInitialize(1, iface)

    pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    pub.Init()
    crc = CRC()
    cmd = init_cmd()

    print("Trot gait - vx=%.2f vy=%.2f vyaw=%.2f%s" % (vx, vy, vyaw, " [arm-hold]" if arm_hold else ""))
    print("Assicurati che il simulatore sia avviato. Ctrl+C per fermare.")
    input("Premi Invio per iniziare...")

    t = 0.0
    try:
        while True:
            step_start = time.perf_counter()
            q = trot_pose(t, vx, vy, vyaw, arm_hold=arm_hold)

            for i in range(12):
                cmd.motor_cmd[i].q = float(q[i])
                if arm_hold:
                    # Gambe posteriori (RR, RL) più potenti per compensare peso braccio
                    kp, kd = (200.0,2.0) if i >= 6 else (45.0, 6.0)
                else:
                    kp, kd = KP, KD
                cmd.motor_cmd[i].kp = kp
                cmd.motor_cmd[i].dq = 0.0
                cmd.motor_cmd[i].kd = kd
                cmd.motor_cmd[i].tau = 0.0

            if arm_hold:
                for i in range(6):
                    cmd.motor_cmd[12 + i].q = 0.0
                    cmd.motor_cmd[12 + i].kp = 0.0
                    cmd.motor_cmd[12 + i].dq = 0.0
                    cmd.motor_cmd[12 + i].kd = 0.0
                    cmd.motor_cmd[12 + i].tau = ARM_HOLD[i]

            cmd.crc = crc.Crc(cmd)
            pub.Write(cmd)

            t += DT
            elapsed = time.perf_counter() - step_start
            if DT - elapsed > 0:
                time.sleep(DT - elapsed)

    except KeyboardInterrupt:
        print("\nFermato.")


if __name__ == "__main__":
    main()

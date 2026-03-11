#!/usr/bin/env python3
"""
Policy open source di camminata: trot gait semplificato per Go2.

Trot = andatura a diagonale: FR+RL e FL+RR si muovono in coppia.
Usa oscillazioni sinusoidali sulle articolazioni per generare il passo.

Uso:
  1. Avvia il simulatore: cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py
  2. Esegui: python3 scripts/trot_gait.py [vx] [vy] [vyaw]
     - vx, vy, vyaw: velocità desiderate (default 0.2, 0, 0)
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

DT = 0.002
KP = 40.0
KD = 3.0
FREQ = 1.2  # Hz - frequenza del trot
SWING_AMP = 0.15   # ampiezza oscillazione thigh (rad)
STANCE_AMP = 0.08  # ampiezza oscillazione calf (rad)


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


def trot_pose(t, vx, vy, vyaw):
    """
    Calcola posizioni articolari per trot.
    FR(0-2), FL(3-5), RR(6-8), RL(9-11)
    Trot: FR+RL in fase, FL+RR in fase opposta.
    """
    phase = 2 * math.pi * FREQ * t
    q = STAND_POSE.copy()

    # Coppie diagonali: FR+RL (phase), FL+RR (phase+pi)
    for leg_offset, phase_off in [(0, 0), (3, math.pi), (6, math.pi), (9, 0)]:
        p = phase + phase_off
        # thigh (indice 1, 4, 7, 10): oscillazione principale
        q[leg_offset + 1] += SWING_AMP * math.sin(p)
        # calf (indice 2, 5, 8, 11): segue thigh
        q[leg_offset + 2] += STANCE_AMP * math.sin(p)

    return q


def main():
    # Parse args: trot_gait.py [vx [vy [vyaw]]] oppure trot_gait.py INTERFACCIA vx vy vyaw
    def is_float(s):
        try:
            float(s)
            return True
        except (ValueError, TypeError):
            return False

    if len(sys.argv) >= 2 and not is_float(sys.argv[1]):
        # Prima arg = interfaccia (robot reale), es: trot_gait.py enp3s0 0.2
        interface = sys.argv[1]
        vx = float(sys.argv[2]) if len(sys.argv) > 2 else 0.2
        vy = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
        vyaw = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
        ChannelFactoryInitialize(0, interface)
        print("Robot reale (interface=%s)" % interface)
    else:
        vx = float(sys.argv[1]) if len(sys.argv) > 1 else 0.2
        vy = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
        vyaw = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
        try:
            sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python"))
            import config as sim_config
            interface = sim_config.INTERFACE
        except ImportError:
            interface = "lo"
        ChannelFactoryInitialize(1, interface)

    pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    pub.Init()
    crc = CRC()
    cmd = init_cmd()

    print("Trot gait - vx=%.2f vy=%.2f vyaw=%.2f" % (vx, vy, vyaw))
    print("Assicurati che il simulatore sia avviato. Ctrl+C per fermare.")
    input("Premi Invio per iniziare...")

    t = 0.0
    try:
        while True:
            step_start = time.perf_counter()
            q = trot_pose(t, vx, vy, vyaw)

            for i in range(12):
                cmd.motor_cmd[i].q = float(q[i])
                cmd.motor_cmd[i].kp = KP
                cmd.motor_cmd[i].dq = 0.0
                cmd.motor_cmd[i].kd = KD
                cmd.motor_cmd[i].tau = 0.0

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

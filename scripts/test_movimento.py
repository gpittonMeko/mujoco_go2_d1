#!/usr/bin/env python3
"""
Script di test per il movimento del Go2 in simulazione MuJoCo.

Esegue una sequenza: stand up -> hold -> leggero squat -> stand down.

Uso:
  1. Avvia il simulatore: cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py
  2. python3 scripts/test_movimento.py [--arm-hold] [--interface lo|lan2] [--no-wait]
     - Non richiede LowState: invia solo LowCmd al simulatore
"""

import time
import sys
import os
import argparse
import numpy as np

# Path progetto
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_sdk2_python"))

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelFactoryInitialize
from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_
from unitree_sdk2py.utils.crc import CRC

# Posizioni articolari Go2 (12 motori: FR, FL, RR, RL x 3 giunti)
STAND_UP = np.array([
    0.006, 0.61, -1.22, -0.006, 0.61, -1.22,
    0.006, 0.61, -1.22, -0.006, 0.61, -1.22
], dtype=float)

STAND_DOWN = np.array([
    0.047, 1.22, -2.44, -0.047, 1.22, -2.44,
    0.047, 1.22, -2.44, -0.047, 1.22, -2.44
], dtype=float)

# Leggero squat (ginocchia più piegate)
SQUAT = np.array([
    0.0, 0.85, -1.7, 0.0, 0.85, -1.7,
    0.0, 0.85, -1.7, 0.0, 0.85, -1.7
], dtype=float)

DT = 0.002
KP_STAND = 50.0
KD = 3.5

# go2_d1: braccio ripiegato (rad) - posizione molto compatta
ARM_HOLD_POSE = [0.0, 0.2, -0.4, -0.2, 0.0, 0.0]
ARM_HOLD_KP = 0.0  # Z1 è position servo: tau=q_des, kp/kd=0
ARM_HOLD_KD = 0.0
NUM_ARM_MOTORS = 6  # go2_d1 ha 18 attuatori


def init_cmd(arm_hold=False):
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
    if arm_hold:
        for i in range(NUM_ARM_MOTORS):
            cmd.motor_cmd[12 + i].q = 0.0
            cmd.motor_cmd[12 + i].kp = 0.0
            cmd.motor_cmd[12 + i].dq = 0.0
            cmd.motor_cmd[12 + i].kd = 0.0
            cmd.motor_cmd[12 + i].tau = ARM_HOLD_POSE[i]
    return cmd


def interpolate(phase, pos_a, pos_b, kp_a, kp_b):
    """Interpolazione smooth tra due pose."""
    q = (1 - phase) * pos_a + phase * pos_b
    kp = (1 - phase) * kp_a + phase * kp_b
    return q, kp


def main():
    parser = argparse.ArgumentParser(description="Test stand movimento Go2")
    parser.add_argument("interface", nargs="?", help="Se fornito: robot reale (es. lan2). Altrimenti: sim con config")
    parser.add_argument("--interface", dest="iface_override", type=str, default=None, help="Override interfaccia per sim (es. lo, lan2)")
    parser.add_argument("--arm-hold", action="store_true", help="Go2_d1: tiene braccio fisso")
    parser.add_argument("--no-wait", action="store_true", help="Avvia subito senza attendere Invio")
    args = parser.parse_args()

    if args.interface is None:
        # Simulazione
        iface = args.iface_override
        if iface is None:
            try:
                sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python"))
                import config as sim_config
                iface = sim_config.INTERFACE
            except ImportError:
                iface = "lo"
        ChannelFactoryInitialize(1, iface)
        print(f"Simulazione (domain_id=1, interface={iface})")
    else:
        ChannelFactoryInitialize(0, args.interface)
        print(f"Robot reale (interface={args.interface})")

    pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    pub.Init()
    crc = CRC()
    cmd = init_cmd(arm_hold=args.arm_hold)

    print("Test movimento Go2 - Sequenza: stand up -> hold -> squat -> stand down" + (" | arm_hold" if args.arm_hold else ""))
    print("Assicurati che il simulatore sia avviato.")
    if not args.no_wait:
        input("Premi Invio per iniziare...")
    else:
        print("Avvio in 2s...")
        time.sleep(2.0)

    t = 0.0
    phase_duration = 1.2

    # Fasi: 0=stand up, 1=hold, 2=squat, 3=hold, 4=stand down
    phase = 0
    phase_t = 0.0

    try:
        while True:
            step_start = time.perf_counter()
            t += DT
            phase_t += DT

            if phase == 0:  # Stand up (0 -> 1.2s)
                p = min(1.0, np.tanh(phase_t / phase_duration))
                q, kp = interpolate(p, STAND_DOWN, STAND_UP, 20.0, KP_STAND)
                if p >= 1.0:
                    phase = 1
                    phase_t = 0.0
                    print("  [Stand up completato]")

            elif phase == 1:  # Hold in piedi (1.5s)
                q, kp = STAND_UP, KP_STAND
                if phase_t >= 1.5:
                    phase = 2
                    phase_t = 0.0
                    print("  [Squat...]")

            elif phase == 2:  # Squat (1.2s)
                p = min(1.0, np.tanh(phase_t / phase_duration))
                q, kp = interpolate(p, STAND_UP, SQUAT, KP_STAND, KP_STAND)
                if p >= 1.0:
                    phase = 3
                    phase_t = 0.0

            elif phase == 3:  # Hold squat (1s)
                q, kp = SQUAT, KP_STAND
                if phase_t >= 1.0:
                    phase = 4
                    phase_t = 0.0
                    print("  [Stand down...]")

            elif phase == 4:  # Stand down (1.2s)
                p = min(1.0, np.tanh(phase_t / phase_duration))
                q, kp = interpolate(p, SQUAT, STAND_DOWN, KP_STAND, 20.0)
                if p >= 1.0:
                    print("  [Test completato]")
                    break

            for i in range(12):
                cmd.motor_cmd[i].q = float(q[i])
                cmd.motor_cmd[i].kp = kp
                cmd.motor_cmd[i].dq = 0.0
                cmd.motor_cmd[i].kd = KD
                cmd.motor_cmd[i].tau = 0.0

            if args.arm_hold:
                for i in range(NUM_ARM_MOTORS):
                    cmd.motor_cmd[12 + i].q = 0.0
                    cmd.motor_cmd[12 + i].kp = 0.0
                    cmd.motor_cmd[12 + i].dq = 0.0
                    cmd.motor_cmd[12 + i].kd = 0.0
                    cmd.motor_cmd[12 + i].tau = ARM_HOLD_POSE[i]

            cmd.crc = crc.Crc(cmd)
            pub.Write(cmd)

            elapsed = time.perf_counter() - step_start
            if DT - elapsed > 0:
                time.sleep(DT - elapsed)

    except KeyboardInterrupt:
        print("\nInterrotto.")

    print("Fine test.")


if __name__ == "__main__":
    main()

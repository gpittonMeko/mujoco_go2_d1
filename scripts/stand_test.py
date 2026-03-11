#!/usr/bin/env python3
"""
Test stand minimale: invia LowCmd al simulatore SENZA attendere LowState.
Utile per verificare che DDS funzioni (simulatore riceve comandi).

Uso:
  1. Avvia il simulatore
  2. python3 scripts/stand_test.py [--arm-hold] [--interface lo]
"""
import time
import sys
import os
import argparse
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_sdk2_python"))

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelFactoryInitialize
from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_
from unitree_sdk2py.utils.crc import CRC

STAND_UP = np.array([
    0.006, 0.61, -1.22, -0.006, 0.61, -1.22,
    0.006, 0.61, -1.22, -0.006, 0.61, -1.22
], dtype=float)

STAND_DOWN = np.array([
    0.047, 1.22, -2.44, -0.047, 1.22, -2.44,
    0.047, 1.22, -2.44, -0.047, 1.22, -2.44
], dtype=float)

DT = 0.002
KP = 50.0
KD = 3.5
# Braccio ripiegato (rad) - posizione molto compatta
ARM_HOLD = [0.0, 0.2, -0.4, -0.2, 0.0, 0.0]
ARM_KP, ARM_KD = 0.0, 0.0  # Z1 è position servo: tau=q_des, kp/kd=0


def main():
    parser = argparse.ArgumentParser(description="Stand test - solo LowCmd, nessun LowState")
    parser.add_argument("--interface", type=str, default=None)
    parser.add_argument("--arm-hold", action="store_true")
    args = parser.parse_args()

    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python"))
        import config as sim_config
        iface = args.interface or sim_config.INTERFACE
    except ImportError:
        iface = args.interface or "lo"

    ChannelFactoryInitialize(1, iface)
    print(f"Stand test (domain=1, interface={iface}) - NON richiede LowState")
    print("Avvia il simulatore, poi questo script. Stand up in 2s...")

    pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    pub.Init()
    crc = CRC()

    cmd = unitree_go_msg_dds__LowCmd_()
    cmd.head[0], cmd.head[1] = 0xFE, 0xEF
    cmd.level_flag, cmd.gpio = 0xFF, 0
    for i in range(20):
        cmd.motor_cmd[i].mode = 0x01
        cmd.motor_cmd[i].q = cmd.motor_cmd[i].kp = cmd.motor_cmd[i].dq = cmd.motor_cmd[i].kd = cmd.motor_cmd[i].tau = 0.0

    time.sleep(2.0)
    phase_dur = 1.2
    t = 0.0

    try:
        while True:
            step_start = time.perf_counter()
            t += DT
            phase = np.tanh(t / phase_dur)
            q = (1 - phase) * STAND_DOWN + phase * STAND_UP
            kp = (1 - phase) * 20.0 + phase * KP

            for i in range(12):
                cmd.motor_cmd[i].q = float(q[i])
                cmd.motor_cmd[i].kp = kp
                cmd.motor_cmd[i].dq = 0.0
                cmd.motor_cmd[i].kd = KD
                cmd.motor_cmd[i].tau = 0.0

            if args.arm_hold:
                for i in range(6):
                    cmd.motor_cmd[12 + i].q = 0.0
                    cmd.motor_cmd[12 + i].kp = 0.0
                    cmd.motor_cmd[12 + i].dq = 0.0
                    cmd.motor_cmd[12 + i].kd = 0.0
                    cmd.motor_cmd[12 + i].tau = ARM_HOLD[i]

            cmd.crc = crc.Crc(cmd)
            pub.Write(cmd)

            if t >= phase_dur * 2:
                print("Stand up completato. Ctrl+C per fermare.")
            elapsed = time.perf_counter() - step_start
            if DT - elapsed > 0:
                time.sleep(DT - elapsed)

    except KeyboardInterrupt:
        print("\nFermato.")


if __name__ == "__main__":
    main()

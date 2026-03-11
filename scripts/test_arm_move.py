#!/usr/bin/env python3
"""
Test minimale: fa oscillare J1 del braccio per verificare che i comandi arrivino.
1. Avvia simulatore (unitree_mujoco.py)
2. python3 scripts/test_arm_move.py
Se J1 si muove, il problema è in arm_control. Altrimenti è bridge/DDS.
"""
import time
import math
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_sdk2_python"))

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelFactoryInitialize
from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_
from unitree_sdk2py.utils.crc import CRC

# Gambe in stand (da arm_hold)
STAND_LEGS = [0.0, 0.76, -1.44, 0.0, 0.76, -1.44, 0.0, 1.08, -1.82, 0.0, 1.08, -1.82]
ARM_FOLD = [0.0, 0.2, -0.4, -0.2, 0.0, 0.0]

def main():
    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python"))
        import config as sim_config
        ChannelFactoryInitialize(1, sim_config.INTERFACE)
        iface = sim_config.INTERFACE
    except ImportError:
        ChannelFactoryInitialize(1, "lo")
        iface = "lo"

    pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    pub.Init()
    crc = CRC()

    print("Test oscillazione J1 - 5 secondi. Controlla se il braccio si muove.")
    print(f"Domain=1, interface={iface}")
    input("Premi Invio...")

    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 5.0:
        t = time.perf_counter() - t0
        j1_target = 0.4 * math.sin(2 * math.pi * 0.5 * t)  # ±0.4 rad, 0.5 Hz

        cmd = unitree_go_msg_dds__LowCmd_()
        cmd.head[0], cmd.head[1] = 0xFE, 0xEF
        cmd.level_flag, cmd.gpio = 0xFF, 0
        for i in range(20):
            cmd.motor_cmd[i].mode = 0x01
            cmd.motor_cmd[i].q = cmd.motor_cmd[i].kp = cmd.motor_cmd[i].dq = cmd.motor_cmd[i].kd = cmd.motor_cmd[i].tau = 0.0

        for i in range(12):
            cmd.motor_cmd[i].q = STAND_LEGS[i]
            cmd.motor_cmd[i].kp = 40.0
            cmd.motor_cmd[i].kd = 8.0

        for i in range(6):
            cmd.motor_cmd[12 + i].q = 0.0
            cmd.motor_cmd[12 + i].kp = 0.0
            cmd.motor_cmd[12 + i].kd = 0.0
            cmd.motor_cmd[12 + i].tau = ARM_FOLD[i] if i > 0 else j1_target

        cmd.crc = crc.Crc(cmd)
        pub.Write(cmd)
        time.sleep(0.002)

    print("Fine. J1 si è mosso?")

if __name__ == "__main__":
    main()

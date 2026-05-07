#!/usr/bin/env python3
"""
Blocca gambe in stand + braccio in posizione fissa.
Nessun LowState richiesto - invia solo LowCmd.

Uso: avvia il simulatore, poi:
  python3 scripts/arm_hold.py [--interface lo]
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

# Gambe: anteriori estese (testa su), posteriori estese, hip offset anti-drift
STAND_LEGS = np.array([
    0.006, 0.82, -1.50,  -0.006, 0.82, -1.50,   # FR, FL: anteriori estese
    0.006, 1.08, -1.82,  -0.006, 1.08, -1.82    # RR, RL: posteriori + hip simmetrici
], dtype=float)

# Braccio Z1 (rad) - molto chiuso su sé stesso
ARM_FOLD = [0.0, 0.2, -0.4, -0.2, 0.0, 0.0]

DT = 0.002


def main():
    parser = argparse.ArgumentParser(description="Blocca gambe + braccio in posizione fissa")
    parser.add_argument("--interface", type=str, default=None)
    args = parser.parse_args()

    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python"))
        import config as sim_config
        iface = args.interface or sim_config.INTERFACE
    except ImportError:
        iface = args.interface or "lo"

    ChannelFactoryInitialize(1, iface)
    print(f"arm_hold (domain=1, interface={iface}) - gambe stand + braccio fold")
    print("Avvia il simulatore. Ctrl+C per fermare.")

    pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    pub.Init()
    crc = CRC()

    cmd = unitree_go_msg_dds__LowCmd_()
    cmd.head[0], cmd.head[1] = 0xFE, 0xEF
    cmd.level_flag, cmd.gpio = 0xFF, 0
    for i in range(20):
        cmd.motor_cmd[i].mode = 0x01
        cmd.motor_cmd[i].q = cmd.motor_cmd[i].kp = cmd.motor_cmd[i].dq = cmd.motor_cmd[i].kd = cmd.motor_cmd[i].tau = 0.0

    try:
        while True:
            step_start = time.perf_counter()

            for i in range(12):
                cmd.motor_cmd[i].q = float(STAND_LEGS[i])
                # go2_d1+Z1: anteriori più potenti (evita lean), limiti modello hip ±23.7 knee ±45.4 Nm
                kp = 55.0 if i >= 6 else 60.0  # anteriori KP più alto
                kd = 7.0 if i >= 6 else 8.0
                cmd.motor_cmd[i].kp = kp
                cmd.motor_cmd[i].dq = 0.0
                cmd.motor_cmd[i].kd = kd
                cmd.motor_cmd[i].tau = 0.0

            # Braccio Z1: l'attuatore è un position servo (gainprm/biasprm nel XML)
            # → mandare q_des come tau, con kp=kd=0, così il bridge passa ctrl=q_des
            #   e l'attuatore interno fa force = 1000*(q_des - q) - 100*dq
            for i in range(6):
                cmd.motor_cmd[12 + i].q = 0.0
                cmd.motor_cmd[12 + i].kp = 0.0
                cmd.motor_cmd[12 + i].dq = 0.0
                cmd.motor_cmd[12 + i].kd = 0.0
                cmd.motor_cmd[12 + i].tau = ARM_FOLD[i]

            cmd.crc = crc.Crc(cmd)
            pub.Write(cmd)

            elapsed = time.perf_counter() - step_start
            if DT - elapsed > 0:
                time.sleep(DT - elapsed)

    except KeyboardInterrupt:
        print("\nFermato.")


if __name__ == "__main__":
    main()

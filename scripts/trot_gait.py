#!/usr/bin/env python3
"""
Policy open source di camminata: trot gait semplificato per Go2.

Trot = andatura a diagonale: FR+RL e FL+RR si muovono in coppia.
Usa oscillazioni sinusoidali sulle articolazioni per generare il passo.

Uso:
  1. Avvia il simulatore: cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py
  2. python3 scripts/trot_gait.py [vx] [vy] [vyaw] [--arm-hold] [--joystick]
     - --joystick: WASD+QE per vx,vy,vyaw in tempo reale
     - --arm-hold: go2_d1 con braccio ripiegato
"""

import time
import sys
import os
import math
import threading
import numpy as np

try:
    import tkinter as tk
except ImportError:
    tk = None

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

# go2_d1+Z1: anteriori più estese (testa su), posteriori estese, hip offset anti-drift
STAND_POSE_ARM = np.array([
    0.006, 0.82, -1.50,  -0.006, 0.82, -1.50,   # FR, FL: anteriori estese (non piegate)
    0.006, 1.08, -1.82,  -0.006, 1.08, -1.82    # RR, RL: posteriori + hip simmetrici
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
    vx scala ampiezza (0=passi piccoli, 0.5=pieni), vyaw aggiunge fase per sterzo.
    """
    phase = 2 * math.pi * FREQ * t
    base = STAND_POSE_ARM if arm_hold else STAND_POSE
    swing = SWING_AMP_ARM if arm_hold else SWING_AMP
    stance = STANCE_AMP_ARM if arm_hold else STANCE_AMP
    # vx scala ampiezza: 0->30%, 0.5->100%
    scale = 0.3 + 0.7 * max(0, min(1, vx / 0.5))
    swing, stance = swing * scale, stance * scale
    q = base.copy()

    # Coppie diagonali: FR+RL (phase), FL+RR (phase+pi). vyaw aggiunge sterzo
    phase_l = phase + vyaw * 0.4   # gambe sinistre
    phase_r = phase - vyaw * 0.4   # gambe destre
    for leg_offset, phase_off, ph in [(0, 0, phase_r), (3, math.pi, phase_l), (6, math.pi, phase_l), (9, 0, phase_r)]:
        p = ph + phase_off
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
    parser.add_argument("--joystick", action="store_true", help="Joystick: WASD+QE per vx,vy,vyaw")
    parser.add_argument("--interface", type=str, default=None)
    args = parser.parse_args()
    vx, vy, vyaw = [args.vx], [args.vy], [args.vyaw]  # liste per aggiornamento da joystick
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

    VX_MAX, VY_MAX, VYAW_MAX = 0.5, 0.3, 0.8
    STEP = 0.1
    stop_event = threading.Event()

    if args.joystick and tk is not None:
        print("Trot gait con joystick - W/S vx, A/D vy, Q/E vyaw, Spazio stop%s" % (" [arm-hold]" if arm_hold else ""))
    else:
        if args.joystick and tk is None:
            print("Joystick richiesto ma tkinter non disponibile. Uso vx/vy/vyaw fissi.")
        print("Trot gait - vx=%.2f vy=%.2f vyaw=%.2f%s" % (vx[0], vy[0], vyaw[0], " [arm-hold]" if arm_hold else ""))
    print("Assicurati che il simulatore sia avviato. Ctrl+C per fermare.")
    input("Premi Invio per iniziare...")

    if args.joystick and tk is not None:
        def on_key(event):
            key = event.keysym.lower()
            def clamp(v, lo, hi): return max(lo, min(hi, v))
            if key == "w": vx[0] = clamp(vx[0] + STEP, -VX_MAX, VX_MAX)
            elif key == "s": vx[0] = clamp(vx[0] - STEP, -VX_MAX, VX_MAX)
            elif key == "a": vy[0] = clamp(vy[0] + STEP, -VY_MAX, VY_MAX)
            elif key == "d": vy[0] = clamp(vy[0] - STEP, -VY_MAX, VY_MAX)
            elif key == "q": vyaw[0] = clamp(vyaw[0] + STEP, -VYAW_MAX, VYAW_MAX)
            elif key == "e": vyaw[0] = clamp(vyaw[0] - STEP, -VYAW_MAX, VYAW_MAX)
            elif key == "space": vx[0], vy[0], vyaw[0] = 0.0, 0.0, 0.0
            lbl["text"] = f"vx: {vx[0]:+.2f}  vy: {vy[0]:+.2f}  vyaw: {vyaw[0]:+.2f}"

        root = tk.Tk()
        root.title("Joystick - Trot")
        root.geometry("400x90")
        lbl = tk.Label(root, text=f"vx: {vx[0]:+.2f}  vy: {vy[0]:+.2f}  vyaw: {vyaw[0]:+.2f}", font=("", 14))
        lbl.pack(pady=12)
        tk.Label(root, text="W/S vx | A/D vy | Q/E vyaw | Spazio stop | Esc chiudi", fg="gray").pack()
        root.bind("<KeyPress>", on_key)
        root.bind("<Escape>", lambda e: (stop_event.set(), root.destroy()))
        root.protocol("WM_DELETE_WINDOW", lambda: (stop_event.set(), root.destroy()))
        root.focus_set()
        joystick_thread = threading.Thread(target=lambda: root.mainloop(), daemon=True)
        joystick_thread.start()

    t = 0.0
    try:
        while True:
            if args.joystick and tk and stop_event.is_set():
                break
            step_start = time.perf_counter()
            q = trot_pose(t, vx[0], vy[0], vyaw[0], arm_hold=arm_hold)

            for i in range(12):
                cmd.motor_cmd[i].q = float(q[i])
                if arm_hold:
                    # go2_d1+Z1: anteriori più potenti (evita lean avanti), valori realistici Go2
                    # Limiti modello: hip ±23.7 Nm, knee ±45.43 Nm
                    kp, kd = (55.0, 7.0) if i >= 6 else (65.0, 8.0)  # anteriori KP più alto
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
        pass
    stop_event.set()
    print("\nFermato.")


if __name__ == "__main__":
    main()

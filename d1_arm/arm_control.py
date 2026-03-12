#!/usr/bin/env python3
"""
Interfaccia virtuale per controllo braccio D1/Z1 su Go2.

Usa gli stessi comandi LowCmd del robot reale (q, kp, kd, dq, tau).
D1 e Z1 condividono il protocollo low-level motor (unitree_sdk2).

Uso:
  1. Avvia simulatore con ROBOT = "go2_d1"
  2. python3 d1_arm/arm_control.py

  Oppure con policy gambe: avvia deploy_policy + arm_control in parallelo
  (arm_control sovrascrive solo i joint 12-17, ma serve merge - per ora
   arm_control è standalone: stand + controllo braccio)
"""

import os
import sys
import time
import threading
import math

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_sdk2_python"))

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.utils.crc import CRC

# Stand pose gambe (da go2_deploy ts_config)
STAND_POSE = [0.0, 0.8, -1.5, 0.0, 0.8, -1.5, 0.0, 0.8, -1.5, 0.0, 0.8, -1.5]

# Pose braccio Z1 (rad): Home, Fold (compatto)
ARM_HOME = [0.0, 0.785, -0.261, -0.523, 0.0, 0.0]
ARM_FOLD = [0.0, 0.2, -0.4, -0.2, 0.0, 0.0]  # molto chiuso su sé stesso

KP_LEGS = 20.0
KD_LEGS = 0.5
KP_ARM = 0.0   # Z1 è position servo: tau=q_des, kp/kd=0
KD_ARM = 0.0

ARM_OFFSET = 12  # motor_cmd[12..17] = braccio


def main():
    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python"))
        import config as sim_config
        ChannelFactoryInitialize(1, sim_config.INTERFACE)
        print(f"Simulazione (domain_id=1, interface={sim_config.INTERFACE})")
    except ImportError:
        ChannelFactoryInitialize(1, "lo")

    arm_pos = list(ARM_HOME)
    low_state = {"msg": None}

    def on_state(msg: LowState_):
        low_state["msg"] = msg

    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(on_state, 10)
    pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    pub.Init()
    crc = CRC()

    try:
        import tkinter as tk
    except ImportError:
        print("tkinter non disponibile. Uso pose Home fissa.")
        while True:
            cmd = make_cmd(arm_pos, crc)
            pub.Write(cmd)
            time.sleep(0.02)
        return

    def make_cmd(arm, crc_obj):
        c = unitree_go_msg_dds__LowCmd_()
        c.head[0], c.head[1] = 0xFE, 0xEF
        c.level_flag, c.gpio = 0xFF, 0
        for i in range(20):
            c.motor_cmd[i].mode = 0x01
            c.motor_cmd[i].q = c.motor_cmd[i].kp = c.motor_cmd[i].dq = c.motor_cmd[i].kd = c.motor_cmd[i].tau = 0.0
        for i in range(12):
            c.motor_cmd[i].q = STAND_POSE[i]
            c.motor_cmd[i].kp = KP_LEGS
            c.motor_cmd[i].kd = KD_LEGS
        for i in range(6):
            c.motor_cmd[ARM_OFFSET + i].q = 0.0
            c.motor_cmd[ARM_OFFSET + i].kp = 0.0
            c.motor_cmd[ARM_OFFSET + i].kd = 0.0
            c.motor_cmd[ARM_OFFSET + i].tau = arm[i]
        c.crc = crc_obj.Crc(c)
        return c

    def publish_loop():
        while True:
            cmd = make_cmd(arm_pos, crc)
            pub.Write(cmd)
            time.sleep(0.002)  # 500 Hz come altri script

    pub_thread = threading.Thread(target=publish_loop, daemon=True)
    pub_thread.start()

    root = tk.Tk()
    root.title("D1/Z1 Arm Control")
    root.geometry("420x320")

    lbl_deg = [None] * 6

    def on_slider(val_deg, idx):
        arm_pos[idx] = math.radians(float(val_deg))
        if lbl_deg[idx]:
            lbl_deg[idx]["text"] = f"{int(float(val_deg))}°"

    sliders = []
    for i in range(6):
        f = tk.Frame(root)
        f.pack(fill=tk.X, padx=8, pady=2)
        tk.Label(f, text=f"J{i+1}", width=3).pack(side=tk.LEFT)
        s = tk.Scale(f, from_=-360, to=360, resolution=5, orient=tk.HORIZONTAL,
                    length=280, command=lambda v, idx=i: on_slider(v, idx),
                    showvalue=False)
        s.set(int(math.degrees(arm_pos[i])))
        s.pack(side=tk.LEFT, fill=tk.X, expand=True)
        lbl = tk.Label(f, text=f"{int(math.degrees(arm_pos[i]))}°", width=5, font=("", 10))
        lbl.pack(side=tk.LEFT)
        lbl_deg[i] = lbl
        sliders.append(s)

    def set_pose(pose):
        for i, v in enumerate(pose):
            arm_pos[i] = v
            d = int(math.degrees(v))
            sliders[i].set(d)
            if lbl_deg[i]:
                lbl_deg[i]["text"] = f"{d}°"

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=8)
    tk.Button(btn_frame, text="Home", command=lambda: set_pose(ARM_HOME)).pack(side=tk.LEFT, padx=4)
    tk.Button(btn_frame, text="Fold", command=lambda: set_pose(ARM_FOLD)).pack(side=tk.LEFT, padx=4)

    def test_oscillate():
        """Oscilla J1 per 3s - verifica che il braccio risponda."""
        orig = arm_pos[0]
        t0 = [time.perf_counter()]

        def _tick():
            elapsed = time.perf_counter() - t0[0]
            if elapsed < 3.0:
                arm_pos[0] = orig + 0.3 * math.sin(2 * math.pi * 0.5 * elapsed)
                sliders[0].set(int(math.degrees(arm_pos[0])))
                if lbl_deg[0]:
                    lbl_deg[0]["text"] = f"{int(math.degrees(arm_pos[0]))}°"
                root.after(20, _tick)
            else:
                arm_pos[0] = orig
                sliders[0].set(int(math.degrees(orig)))
                if lbl_deg[0]:
                    lbl_deg[0]["text"] = f"{int(math.degrees(orig))}°"
        _tick()
    tk.Button(btn_frame, text="Test J1", command=test_oscillate).pack(side=tk.LEFT, padx=4)

    tk.Label(root, text="Comandi LowCmd (tau=pos, kp=kd=0) - Z1 position servo", fg="gray").pack(pady=4)
    root.mainloop()


if __name__ == "__main__":
    main()

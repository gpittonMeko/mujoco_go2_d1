#!/usr/bin/env python3
"""
Launcher unico per go2_d1+Z1: policy RL + joystick + controllo braccio.

Avvia tutto insieme: gambe dalla policy (Teacher-Student), braccio dagli slider,
joystick WASD+QE per vx,vy,vyaw.

Uso:
  1. Avvia simulatore: cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py
  2. python3 scripts/run_go2_d1_z1.py
"""

import time
import sys
import os
import argparse
import threading
import math

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_sdk2_python"))
sys.path.insert(0, PROJECT_ROOT)

import importlib.util
spec = importlib.util.spec_from_file_location(
    "deploy_policy",
    os.path.join(PROJECT_ROOT, "scripts", "deploy_policy.py")
)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
PolicyRunner = deploy.PolicyRunner

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.utils.crc import CRC

try:
    import tkinter as tk
except ImportError:
    tk = None

# Pose braccio Z1 (rad)
ARM_HOME = [0.0, 0.785, -0.261, -0.523, 0.0, 0.0]
ARM_FOLD = [0.0, 0.2, -0.4, -0.2, 0.0, 0.0]
VX_MAX, VY_MAX, VYAW_MAX = 0.8, 0.5, 1.0
STEP_JOY = 0.15


def main():
    parser = argparse.ArgumentParser(description="go2_d1+Z1: policy + joystick + braccio")
    parser.add_argument("--model", default="ts", choices=["ts", "wtw"])
    parser.add_argument("--interface", type=str, default=None)
    args = parser.parse_args()

    models_dir = os.path.join(PROJECT_ROOT, "go2_deploy", "models")
    params_dir = os.path.join(PROJECT_ROOT, "go2_deploy", "params")
    config_path = os.path.join(params_dir, "ts_config.yaml") if args.model == "ts" else os.path.join(params_dir, "wtw_config.yaml")
    model_path = os.path.join(models_dir, "policy_ts_gs.pt") if args.model == "ts" else os.path.join(models_dir, "wtw_model.pt")

    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python"))
        import config as sim_config
        iface = args.interface or sim_config.INTERFACE
    except ImportError:
        iface = args.interface or "lo"
    ChannelFactoryInitialize(1, iface)

    runner = PolicyRunner(config_path, model_path, 0.0, 0.0, 0.0, arm_hold=True)
    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(runner.state_callback, 10)
    pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    pub.Init()
    crc = CRC()

    arm_pos = list(ARM_FOLD)
    arm_lock = threading.Lock()
    stop_event = threading.Event()

    print("go2_d1+Z1: policy + joystick + braccio")
    print("In attesa di lowstate...")
    while runner.low_state is None:
        time.sleep(0.1)
    print("Avvio in 2s...")
    time.sleep(2.0)

    def policy_loop():
        while not stop_event.is_set():
            step_start = time.perf_counter()
            target_pos = runner.step()
            cmd = runner.make_cmd(target_pos)
            with arm_lock:
                ap = list(arm_pos)
            for i in range(6):
                cmd.motor_cmd[12 + i].q = 0.0
                cmd.motor_cmd[12 + i].kp = 0.0
                cmd.motor_cmd[12 + i].dq = 0.0
                cmd.motor_cmd[12 + i].kd = 0.0
                cmd.motor_cmd[12 + i].tau = ap[i]
            cmd.crc = crc.Crc(cmd)
            pub.Write(cmd)
            elapsed = time.perf_counter() - step_start
            if runner.dt - elapsed > 0:
                time.sleep(runner.dt - elapsed)

    policy_thread = threading.Thread(target=policy_loop, daemon=True)
    policy_thread.start()

    if tk is None:
        print("tkinter non disponibile. Policy attiva, braccio in ARM_FOLD. Ctrl+C per fermare.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_event.set()
            print("\nFermato.")
        return

    def clamp(v, lo, hi):
        return max(lo, min(hi, v))

    def on_key(event):
        key = event.keysym.lower()
        vx, vy, vyaw = runner.cmd[0], runner.cmd[1], runner.cmd[2]
        if key == "w": vx = clamp(vx + STEP_JOY, -VX_MAX, VX_MAX)
        elif key == "s": vx = clamp(vx - STEP_JOY, -VX_MAX, VX_MAX)
        elif key == "a": vy = clamp(vy + STEP_JOY, -VY_MAX, VY_MAX)
        elif key == "d": vy = clamp(vy - STEP_JOY, -VY_MAX, VY_MAX)
        elif key == "q": vyaw = clamp(vyaw + STEP_JOY, -VYAW_MAX, VYAW_MAX)
        elif key == "e": vyaw = clamp(vyaw - STEP_JOY, -VYAW_MAX, VYAW_MAX)
        elif key == "space": vx, vy, vyaw = 0.0, 0.0, 0.0
        runner.cmd[0], runner.cmd[1], runner.cmd[2] = vx, vy, vyaw
        lbl_joy["text"] = f"vx: {vx:+.2f}  vy: {vy:+.2f}  vyaw: {vyaw:+.2f}"

    root = tk.Tk()
    root.title("go2_d1+Z1 - Policy + Braccio")
    root.geometry("440x380")

    # Joystick
    f_joy = tk.LabelFrame(root, text="Joystick (W/S vx, A/D vy, Q/E vyaw, Spazio stop)", padx=10, pady=5)
    f_joy.pack(fill=tk.X, padx=8, pady=4)
    lbl_joy = tk.Label(f_joy, text="vx: +0.00  vy: +0.00  vyaw: +0.00", font=("", 12))
    lbl_joy.pack()

    # Braccio
    f_arm = tk.LabelFrame(root, text="Braccio Z1 (J1-J6)", padx=10, pady=5)
    f_arm.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
    lbl_deg = [None] * 6  # label che mostra gradi accanto a ogni slider

    def on_slider(val_deg, idx):
        with arm_lock:
            arm_pos[idx] = math.radians(float(val_deg))
        if lbl_deg[idx]:
            lbl_deg[idx]["text"] = f"{int(float(val_deg))}°"

    sliders = []
    for i in range(6):
        fr = tk.Frame(f_arm)
        fr.pack(fill=tk.X, pady=2)
        tk.Label(fr, text=f"J{i+1}", width=3).pack(side=tk.LEFT)
        s = tk.Scale(fr, from_=-360, to=360, resolution=5, orient=tk.HORIZONTAL,
                    length=260, command=lambda v, idx=i: on_slider(v, idx),
                    showvalue=False)  # nasconde il numero predefinito
        s.set(int(math.degrees(arm_pos[i])))
        s.pack(side=tk.LEFT, fill=tk.X, expand=True)
        lbl = tk.Label(fr, text=f"{int(math.degrees(arm_pos[i]))}°", width=5, font=("", 10))
        lbl.pack(side=tk.LEFT)
        lbl_deg[i] = lbl
        sliders.append(s)

    def set_pose(pose):
        for i, v in enumerate(pose):
            with arm_lock:
                arm_pos[i] = v
            d = int(math.degrees(v))
            sliders[i].set(d)
            if lbl_deg[i]:
                lbl_deg[i]["text"] = f"{d}°"

    btn = tk.Frame(f_arm)
    btn.pack(pady=6)
    tk.Button(btn, text="Home", command=lambda: set_pose(ARM_HOME)).pack(side=tk.LEFT, padx=4)
    tk.Button(btn, text="Fold", command=lambda: set_pose(ARM_FOLD)).pack(side=tk.LEFT, padx=4)

    root.bind("<KeyPress>", on_key)
    root.bind("<Escape>", lambda e: (stop_event.set(), root.destroy()))
    root.protocol("WM_DELETE_WINDOW", lambda: (stop_event.set(), root.destroy()))
    root.focus_set()
    root.mainloop()
    stop_event.set()
    print("\nFermato.")


if __name__ == "__main__":
    main()

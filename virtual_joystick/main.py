#!/usr/bin/env python3
"""
Joystick virtuale per controllo movimento Go2.
Usa tastiera (WASD + QE) per vx, vy, vyaw e integra la policy RL.
"""

import time
import sys
import os
import argparse
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_sdk2_python"))

# Importa PolicyRunner da deploy_policy
import importlib.util
spec = importlib.util.spec_from_file_location(
    "deploy_policy",
    os.path.join(PROJECT_ROOT, "scripts", "deploy_policy.py")
)
deploy = importlib.util.module_from_spec(spec)
sys.path.insert(0, PROJECT_ROOT)
spec.loader.exec_module(deploy)
PolicyRunner = deploy.PolicyRunner

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_

try:
    import tkinter as tk
except ImportError:
    tk = None


# Velocità massime e incremento
VX_MAX = 0.8
VY_MAX = 0.5
VYAW_MAX = 1.0
STEP = 0.15


def run_policy_loop(runner, pub, stop_event):
    """Loop policy in thread separato."""
    while not stop_event.is_set():
        step_start = time.perf_counter()
        target_pos = runner.step()
        cmd = runner.make_cmd(target_pos)
        pub.Write(cmd)
        elapsed = time.perf_counter() - step_start
        sleep_time = runner.dt - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)


def main():
    parser = argparse.ArgumentParser(description="Joystick virtuale per Go2")
    parser.add_argument("--model", default="ts", choices=["ts", "wtw"])
    parser.add_argument("--arm-hold", action="store_true", help="Go2_d1: braccio fisso in pose 0")
    parser.add_argument("--interface", type=str, default=None)
    args = parser.parse_args()

    models_dir = os.path.join(PROJECT_ROOT, "go2_deploy", "models")
    params_dir = os.path.join(PROJECT_ROOT, "go2_deploy", "params")

    if args.model == "ts":
        config_path = os.path.join(params_dir, "ts_config.yaml")
        model_path = os.path.join(models_dir, "policy_ts_gs.pt")
    else:
        config_path = os.path.join(params_dir, "wtw_config.yaml")
        model_path = os.path.join(models_dir, "wtw_model.pt")

    if args.interface:
        ChannelFactoryInitialize(0, args.interface)
    else:
        try:
            sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python"))
            import config as sim_config
            ChannelFactoryInitialize(1, sim_config.INTERFACE)
            print(f"Simulazione (domain_id=1, interface={sim_config.INTERFACE})")
        except ImportError:
            ChannelFactoryInitialize(1, "lo")

    runner = PolicyRunner(config_path, model_path, 0.0, 0.0, 0.0, arm_hold=args.arm_hold)

    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(runner.state_callback, 10)

    pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    pub.Init()

    print("In attesa di lowstate dal simulatore...")
    while runner.low_state is None:
        time.sleep(0.1)
    print("Stato ricevuto. Avvio in 2s...")
    time.sleep(2.0)

    stop_event = threading.Event()
    policy_thread = threading.Thread(target=run_policy_loop, args=(runner, pub, stop_event))
    policy_thread.daemon = True
    policy_thread.start()

    def clamp(v, lo, hi):
        return max(lo, min(hi, v))

    def on_key(event):
        vx, vy, vyaw = runner.cmd[0], runner.cmd[1], runner.cmd[2]
        key = event.keysym.lower()
        if key == "w":
            vx = clamp(vx + STEP, -VX_MAX, VX_MAX)
        elif key == "s":
            vx = clamp(vx - STEP, -VX_MAX, VX_MAX)
        elif key == "a":
            vy = clamp(vy + STEP, -VY_MAX, VY_MAX)
        elif key == "d":
            vy = clamp(vy - STEP, -VY_MAX, VY_MAX)
        elif key == "q":
            vyaw = clamp(vyaw + STEP, -VYAW_MAX, VYAW_MAX)
        elif key == "e":
            vyaw = clamp(vyaw - STEP, -VYAW_MAX, VYAW_MAX)
        elif key == "space":
            vx, vy, vyaw = 0.0, 0.0, 0.0
        runner.cmd[0], runner.cmd[1], runner.cmd[2] = vx, vy, vyaw
        update_label()

    def update_label():
        vx, vy, vyaw = runner.cmd[0], runner.cmd[1], runner.cmd[2]
        lbl["text"] = f"vx: {vx:+.2f}  vy: {vy:+.2f}  vyaw: {vyaw:+.2f}"

    def on_closing():
        stop_event.set()
        root.destroy()
        sys.exit(0)

    if tk is not None:
        root = tk.Tk()
        root.title("Virtual Joystick - Go2")
        root.geometry("400x120")
        root.resizable(False, False)

        frame = tk.Frame(root, padx=20, pady=20)
        frame.pack(fill=tk.BOTH, expand=True)

        lbl = tk.Label(frame, text="vx: +0.00  vy: +0.00  vyaw: +0.00", font=("", 14))
        lbl.pack(pady=(0, 10))

        hint = tk.Label(
            frame,
            text="W/S vx | A/D vy | Q/E vyaw | Spazio stop | Esc chiudi",
            font=("", 10),
            fg="gray",
        )
        hint.pack()

        root.bind("<KeyPress>", on_key)
        root.bind("<Escape>", lambda e: on_closing())
        root.protocol("WM_DELETE_WINDOW", on_closing)
        root.focus_set()

        update_label()
        root.mainloop()
    else:
        print("Errore: tkinter non disponibile. Installa python3-tk per la GUI.")
        stop_event.set()
        sys.exit(1)


if __name__ == "__main__":
    main()

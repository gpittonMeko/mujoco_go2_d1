#!/usr/bin/env python3
"""
Deploy di policy RL (Teacher-Student) per Go2 in unitree_mujoco Python.

Legge rt/lowstate, costruisce osservazione, inferisce la rete, pubblica rt/lowcmd.

Uso:
  1. Avvia il simulatore: cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py
  2. Go2 plain:  python3 scripts/deploy_policy.py --model ts --vx 0.5
  3. go2_d1+Z1: python3 scripts/deploy_policy.py --model ts --vx 0.5 --arm-hold
  4. Con joystick: python3 scripts/deploy_policy.py --model ts --arm-hold --joystick
     (W/S vx, A/D vy, Q/E vyaw, Spazio stop)
"""

import time
import sys
import os
import argparse
import math
import threading
import numpy as np
from collections import deque

try:
    import tkinter as tk
except ImportError:
    tk = None

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_sdk2_python"))

import torch
import yaml

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowCmd_
from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowState_
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.utils.crc import CRC


def get_gravity_orientation(quat):
    """Proietta il vettore gravità nel frame del robot dato il quaternione (w,x,y,z)."""
    qw, qx, qy, qz = quat
    gx = 2.0 * (-qz * qx + qw * qy)
    gy = -2.0 * (qz * qy + qw * qx)
    gz = 1.0 - 2.0 * (qw * qw + qz * qz)
    return np.array([gx, gy, gz], dtype=np.float32)


# --- go2_d1 con braccio Z1 (--arm-hold) ---
# Pose braccio Z1 ripiegato (rad) - position servo: tau=q_des, kp=kd=0
ARM_HOLD_POSE = [0.0, 0.2, -0.4, -0.2, 0.0, 0.0]
ARM_HOLD_KP = 0.0
ARM_HOLD_KD = 0.0

# Offset gambe per go2_d1: compensa forward lean da peso Z1 (solo con --arm-hold)
# Go2 plain: nessun offset. go2_d1+Z1: anteriori estese, posteriori piegate → corpo più dritto
# Ordine: FR(0-2), FL(3-5), RR(6-8), RL(9-11) - [hip, thigh, calf] per gamba
LEG_OFFSET_GO2_Z1 = np.array([
    0.0, 0.04, -0.03,  0.0, 0.04, -0.03,   # FR, FL: anteriori leggermente estese
    0.0, -0.04, 0.03,  0.0, -0.04, 0.03    # RR, RL: posteriori leggermente piegate
], dtype=np.float32)

# go2_d1 + D1 mesh: braccio sul dorso tende a pitch in avanti → carico sulle zampe anteriori.
# Trim simmetrico: anteriori leggermente meno “in avanti”, posteriori un po’ più carichi (tuning grossolano).
# Ordine: FR, FL, RR, RL × [hip, thigh, calf]
LEG_TRIM_GO2_D1 = np.array([
    0.0, -0.035, 0.03, 0.0, -0.035, 0.03,
    0.0, 0.05, -0.04, 0.0, 0.05, -0.04,
], dtype=np.float32)


class PolicyRunner:
    def __init__(
        self,
        config_path,
        model_path,
        vx=0.5,
        vy=0.0,
        vyaw=0.0,
        arm_hold=False,
        leg_ctrl_kp=None,
        leg_ctrl_kd=None,
        leg_trim=None,
    ):
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f)

        self.model = torch.jit.load(model_path)
        self.model.eval()

        self.dt = cfg["dt"]
        self.ctrl_kp = float(leg_ctrl_kp) if leg_ctrl_kp is not None else float(cfg["ctrl_kp"])
        self.ctrl_kd = float(leg_ctrl_kd) if leg_ctrl_kd is not None else float(cfg["ctrl_kd"])
        self.stand_kp = cfg["stand_kp"]
        self.stand_kd = cfg["stand_kd"]
        self.action_scale = cfg["action_scale"]
        self.lin_vel_scale = cfg["lin_vel_scale"]
        self.ang_vel_scale = cfg["ang_vel_scale"]
        self.dof_pos_scale = cfg["dof_pos_scale"]
        self.dof_vel_scale = cfg["dof_vel_scale"]
        self.default_pos = np.array(cfg["stand_pos"], dtype=np.float32)

        self.num_single_obs = cfg["num_single_obs"]
        self.frame_stack = cfg["frame_stack"]
        self.is_wtw = "theta_fl" in cfg
        self.has_ts_history = not self.is_wtw

        self.cmd = np.array([vx, vy, vyaw], dtype=np.float32)
        self.actions = np.zeros(12, dtype=np.float32)

        # WTW-specific
        if self.is_wtw:
            self.num_gaits = cfg.get("num_gaits", 4)
            self.gait_period = 0.5
            self.base_height_target = 0.28
            self.foot_clearance_target = 0.08
            self.pitch_target = 0.0
            self.theta_fl = np.array(cfg["theta_fl"], dtype=np.float32)
            self.theta_fr = np.array(cfg["theta_fr"], dtype=np.float32)
            self.theta_rl = np.array(cfg["theta_rl"], dtype=np.float32)
            self.theta_rr = np.array(cfg["theta_rr"], dtype=np.float32)
            self.gait_index = 0  # trot by default

        self.history = deque(maxlen=self.frame_stack)
        for _ in range(self.frame_stack):
            self.history.append(np.zeros(self.num_single_obs, dtype=np.float32))

        self.counter = 0
        self.arm_hold = arm_hold
        self.leg_trim = (
            np.zeros(12, dtype=np.float32)
            if leg_trim is None
            else np.asarray(leg_trim, dtype=np.float32).reshape(12)
        )

        self.low_state = None
        self.crc = CRC()

    def state_callback(self, msg: LowState_):
        self.low_state = msg

    def build_obs_ts(self):
        """Build observation for Teacher-Student model (45 dims)."""
        if self.low_state is None:
            return np.zeros(self.num_single_obs, dtype=np.float32)

        quat = np.array(self.low_state.imu_state.quaternion, dtype=np.float32)
        gyro = np.array(self.low_state.imu_state.gyroscope, dtype=np.float32)
        proj_grav = get_gravity_orientation(quat)

        jpos = np.zeros(12, dtype=np.float32)
        jvel = np.zeros(12, dtype=np.float32)
        for i in range(12):
            jpos[i] = self.low_state.motor_state[i].q
            jvel[i] = self.low_state.motor_state[i].dq

        jpos_processed = jpos - self.default_pos

        obs = np.zeros(self.num_single_obs, dtype=np.float32)
        obs[0] = self.cmd[0] * self.lin_vel_scale
        obs[1] = self.cmd[1] * self.lin_vel_scale
        obs[2] = self.cmd[2] * self.ang_vel_scale
        obs[3:6] = proj_grav
        obs[6:9] = gyro * self.ang_vel_scale
        obs[9:21] = jpos_processed * self.dof_pos_scale
        obs[21:33] = jvel * self.dof_vel_scale
        obs[33:45] = self.actions
        return obs

    def build_obs_wtw(self):
        """Build observation for Walk-These-Ways model (61 dims)."""
        if self.low_state is None:
            return np.zeros(self.num_single_obs, dtype=np.float32)

        quat = np.array(self.low_state.imu_state.quaternion, dtype=np.float32)
        gyro = np.array(self.low_state.imu_state.gyroscope, dtype=np.float32)
        proj_grav = get_gravity_orientation(quat)

        jpos = np.zeros(12, dtype=np.float32)
        jvel = np.zeros(12, dtype=np.float32)
        for i in range(12):
            jpos[i] = self.low_state.motor_state[i].q
            jvel[i] = self.low_state.motor_state[i].dq

        jpos_processed = jpos - self.default_pos

        t = self.counter * self.dt
        phase = (t % self.gait_period) / self.gait_period
        theta = [self.theta_fl, self.theta_fr, self.theta_rl, self.theta_rr]
        clock_input = np.zeros(8, dtype=np.float32)
        for i in range(4):
            th = theta[i][self.gait_index]
            clock_input[i] = math.sin(2 * math.pi * (phase + th))
            clock_input[i + 4] = math.cos(2 * math.pi * (phase + th))

        obs = np.zeros(self.num_single_obs, dtype=np.float32)
        obs[0] = self.cmd[0] * self.lin_vel_scale
        obs[1] = self.cmd[1] * self.lin_vel_scale
        obs[2] = self.cmd[2] * self.ang_vel_scale
        obs[3:6] = proj_grav
        obs[6:9] = gyro * self.ang_vel_scale
        obs[9:21] = jpos_processed * self.dof_pos_scale
        obs[21:33] = jvel * self.dof_vel_scale
        obs[33:45] = self.actions
        obs[45:49] = clock_input[:4]
        obs[49:53] = clock_input[4:]
        obs[53] = self.gait_period
        obs[54] = self.base_height_target
        obs[55] = self.foot_clearance_target
        obs[56] = self.pitch_target
        obs[57:61] = [self.theta_fl[self.gait_index], self.theta_fr[self.gait_index],
                      self.theta_rl[self.gait_index], self.theta_rr[self.gait_index]]
        return obs

    def step(self):
        if self.is_wtw:
            obs = self.build_obs_wtw()
        else:
            obs = self.build_obs_ts()

        self.history.append(obs.copy())

        obs_tensor = torch.from_numpy(obs).unsqueeze(0)

        if self.has_ts_history:
            hist_flat = np.concatenate(list(self.history))
            hist_tensor = torch.from_numpy(hist_flat).unsqueeze(0)
            with torch.no_grad():
                action_tensor = self.model(obs_tensor, hist_tensor)
        else:
            full_obs = np.concatenate(list(self.history))
            full_tensor = torch.from_numpy(full_obs).unsqueeze(0)
            with torch.no_grad():
                action_tensor = self.model(full_tensor)

        self.actions = action_tensor.squeeze(0).numpy()
        target_pos = self.actions * self.action_scale + self.default_pos

        self.counter += 1
        return target_pos

    def make_cmd(self, target_pos):
        # leg_trim: compensazione statica (es. D1). arm_hold: ulteriore offset solo Z1.
        pos = np.asarray(target_pos, dtype=np.float32) + self.leg_trim
        if self.arm_hold:
            pos = pos + LEG_OFFSET_GO2_Z1

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
        for i in range(12):
            cmd.motor_cmd[i].q = float(pos[i])
            cmd.motor_cmd[i].kp = self.ctrl_kp
            cmd.motor_cmd[i].dq = 0.0
            cmd.motor_cmd[i].kd = self.ctrl_kd
            cmd.motor_cmd[i].tau = 0.0
        if self.arm_hold:
            for i in range(6):
                cmd.motor_cmd[12 + i].q = 0.0
                cmd.motor_cmd[12 + i].kp = 0.0
                cmd.motor_cmd[12 + i].dq = 0.0
                cmd.motor_cmd[12 + i].kd = 0.0
                cmd.motor_cmd[12 + i].tau = ARM_HOLD_POSE[i]
        cmd.crc = self.crc.Crc(cmd)
        return cmd


def main():
    parser = argparse.ArgumentParser(description="Deploy RL policy per Go2")
    parser.add_argument("--model", default="ts", choices=["ts", "wtw"], help="ts=Teacher-Student, wtw=Walk-These-Ways")
    parser.add_argument("--vx", type=float, default=0.5)
    parser.add_argument("--vy", type=float, default=0.0)
    parser.add_argument("--vyaw", type=float, default=0.0)
    parser.add_argument("--arm-hold", action="store_true", help="go2_d1+Z1: braccio ripiegato + offset gambe anti-lean (Go2 plain: omettere)")
    parser.add_argument("--joystick", action="store_true", help="Joystick virtuale: WASD+QE per vx,vy,vyaw")
    parser.add_argument("--interface", type=str, default=None, help="Override interfaccia DDS (es. lo, lan2)")
    args = parser.parse_args()

    models_dir = os.path.join(PROJECT_ROOT, "go2_deploy", "models")
    params_dir = os.path.join(PROJECT_ROOT, "go2_deploy", "params")

    if args.model == "ts":
        config_path = os.path.join(params_dir, "ts_config.yaml")
        model_path = os.path.join(models_dir, "policy_ts_gs.pt")
    else:
        config_path = os.path.join(params_dir, "wtw_config.yaml")
        model_path = os.path.join(models_dir, "wtw_model.pt")

    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python"))
        import config as sim_config
        iface = args.interface if args.interface else sim_config.INTERFACE
        ChannelFactoryInitialize(1, iface)
        print(f"Simulazione (domain_id=1, interface={iface})")
    except ImportError:
        iface = args.interface or "lo"
        ChannelFactoryInitialize(1, iface)

    runner = PolicyRunner(config_path, model_path, args.vx, args.vy, args.vyaw, arm_hold=args.arm_hold)

    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(runner.state_callback, 10)

    pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    pub.Init()

    print(f"Policy: {args.model}" + (" | go2_d1+Z1 (arm_hold)" if args.arm_hold else " | Go2 plain") + (" | joystick" if args.joystick else f" | vx={args.vx} vy={args.vy} vyaw={args.vyaw}"))
    print("In attesa di lowstate dal simulatore...")

    while runner.low_state is None:
        time.sleep(0.1)
    print("Stato ricevuto. Avvio policy in 2s...")
    time.sleep(2.0)

    VX_MAX, VY_MAX, VYAW_MAX = 0.8, 0.5, 1.0
    STEP = 0.15
    stop_event = threading.Event()

    def policy_loop():
        while not stop_event.is_set():
            step_start = time.perf_counter()
            target_pos = runner.step()
            c = runner.make_cmd(target_pos)
            pub.Write(c)
            elapsed = time.perf_counter() - step_start
            if runner.dt - elapsed > 0:
                time.sleep(runner.dt - elapsed)

    if args.joystick and tk is not None:
        policy_thread = threading.Thread(target=policy_loop, daemon=True)
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
            lbl["text"] = f"vx: {vx:+.2f}  vy: {vy:+.2f}  vyaw: {vyaw:+.2f}"

        root = tk.Tk()
        root.title("Joystick - Go2")
        root.geometry("420x100")
        lbl = tk.Label(root, text="vx: +0.00  vy: +0.00  vyaw: +0.00", font=("", 14))
        lbl.pack(pady=15, padx=20)
        tk.Label(root, text="W/S vx | A/D vy | Q/E vyaw | Spazio stop | Esc chiudi", fg="gray").pack()
        root.bind("<KeyPress>", on_key)
        root.bind("<Escape>", lambda e: (stop_event.set(), root.destroy()))
        root.protocol("WM_DELETE_WINDOW", lambda: (stop_event.set(), root.destroy()))
        root.focus_set()
        lbl["text"] = f"vx: {runner.cmd[0]:+.2f}  vy: {runner.cmd[1]:+.2f}  vyaw: {runner.cmd[2]:+.2f}"
        root.mainloop()
        stop_event.set()
    else:
        if args.joystick and tk is None:
            print("Joystick richiesto ma tkinter non disponibile. Uso vx/vy/vyaw fissi.")
        print("Policy attiva! Ctrl+C per fermare.")
        try:
            policy_loop()
        except KeyboardInterrupt:
            print("\nFermato.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Deploy di policy RL (Teacher-Student) per Go2 in unitree_mujoco Python.

Legge rt/lowstate, costruisce osservazione, inferisce la rete, pubblica rt/lowcmd.

Uso:
  1. Avvia il simulatore: cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py
  2. python3 scripts/deploy_policy.py [--model ts|wtw] [--vx 0.5] [--vy 0] [--vyaw 0]
"""

import time
import sys
import os
import argparse
import math
import numpy as np
from collections import deque

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


class PolicyRunner:
    def __init__(self, config_path, model_path, vx=0.5, vy=0.0, vyaw=0.0):
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f)

        self.model = torch.jit.load(model_path)
        self.model.eval()

        self.dt = cfg["dt"]
        self.ctrl_kp = cfg["ctrl_kp"]
        self.ctrl_kd = cfg["ctrl_kd"]
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
            cmd.motor_cmd[i].q = float(target_pos[i])
            cmd.motor_cmd[i].kp = self.ctrl_kp
            cmd.motor_cmd[i].dq = 0.0
            cmd.motor_cmd[i].kd = self.ctrl_kd
            cmd.motor_cmd[i].tau = 0.0
        cmd.crc = self.crc.Crc(cmd)
        return cmd


def main():
    parser = argparse.ArgumentParser(description="Deploy RL policy per Go2")
    parser.add_argument("--model", default="ts", choices=["ts", "wtw"], help="ts=Teacher-Student, wtw=Walk-These-Ways")
    parser.add_argument("--vx", type=float, default=0.5)
    parser.add_argument("--vy", type=float, default=0.0)
    parser.add_argument("--vyaw", type=float, default=0.0)
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

    runner = PolicyRunner(config_path, model_path, args.vx, args.vy, args.vyaw)

    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(runner.state_callback, 10)

    pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    pub.Init()

    print(f"Policy: {args.model} | vx={args.vx} vy={args.vy} vyaw={args.vyaw}")
    print("In attesa di lowstate dal simulatore...")

    while runner.low_state is None:
        time.sleep(0.1)
    print("Stato ricevuto. Avvio policy in 2s...")
    time.sleep(2.0)

    print("Policy attiva! Ctrl+C per fermare.")
    try:
        while True:
            step_start = time.perf_counter()
            target_pos = runner.step()
            cmd = runner.make_cmd(target_pos)
            pub.Write(cmd)
            elapsed = time.perf_counter() - step_start
            sleep_time = runner.dt - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
    except KeyboardInterrupt:
        print("\nFermato.")


if __name__ == "__main__":
    main()

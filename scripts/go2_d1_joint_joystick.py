#!/usr/bin/env python3
"""
Joystick manuale per tutti i giunti Go2 + braccio Z1 (18 motori in sim `go2_d1`).

Gambe: comando in posizione (q, kp, kd) come `deploy_policy.py`.
Braccio: attuatori <general> — tau = q_des, kp = kd = 0 (come `run_go2_d1_ball.py`).

Avvio:
  1. Simulatore: cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py
  2. Questo script: python3 scripts/go2_d1_joint_joystick.py [--interface lo]

Tasti: barre = target giunto (rad); «Sync» copia la posa attuale; «Stand» pose gambe da policy + braccio ripiegato.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_sdk2_python"))

try:
    import tkinter as tk
except ImportError:
    print("Serve tkinter: sudo apt install python3-tk")
    sys.exit(1)

import numpy as np
import yaml

from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelPublisher, ChannelSubscriber
from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.utils.crc import CRC

# Limiti (rad) da go2_d1.xml — ordine motori: FR, FL, RR, RL × [hip, thigh, calf]
LEG_LIMITS = [
    (-1.0472, 1.0472),
    (-1.5708, 3.4907),
    (-2.7227, -0.83776),
    (-1.0472, 1.0472),
    (-1.5708, 3.4907),
    (-2.7227, -0.83776),
    (-1.0472, 1.0472),
    (-0.5236, 4.5379),
    (-2.7227, -0.83776),
    (-1.0472, 1.0472),
    (-0.5236, 4.5379),
    (-2.7227, -0.83776),
]
LEG_NAMES = [
    "FR hip",
    "FR thigh",
    "FR calf",
    "FL hip",
    "FL thigh",
    "FL calf",
    "RR hip",
    "RR thigh",
    "RR calf",
    "RL hip",
    "RL thigh",
    "RL calf",
]

# Braccio Z1 (arm_kinematics.J_LIMITS)
ARM_LIMITS = [
    (-2.61799, 2.61799),
    (0.0, 2.96706),
    (-2.87979, 0.0),
    (-1.51844, 1.51844),
    (-1.3439, 1.3439),
    (-2.79253, 2.79253),
]
ARM_NAMES = [
    "arm J1",
    "arm J2",
    "arm J3",
    "arm J4",
    "arm J5",
    "arm J6",
]

NUM_LEG = 12
NUM_ARM = 6
NUM_MOT = NUM_LEG + NUM_ARM

# Pose braccio ripiegata (come deploy_policy / run_go2_d1_ball default Z1)
ARM_FOLD = [0.0, 0.2, -0.4, -0.2, 0.0, 0.0]


def _load_stand_pos():
    ypath = os.path.join(PROJECT_ROOT, "go2_deploy", "params", "ts_config.yaml")
    try:
        with open(ypath, "r") as f:
            cfg = yaml.safe_load(f)
        return np.array(cfg["stand_pos"], dtype=np.float64)
    except Exception:
        return np.array(
            [0.0, 0.8, -1.5] * 4,
            dtype=np.float64,
        )


STAND_LEG = _load_stand_pos()


def _sim_interface(cli_iface):
    try:
        sp = os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python")
        if sp not in sys.path:
            sys.path.insert(0, sp)
        import config as sim_cfg

        return cli_iface or getattr(sim_cfg, "INTERFACE", "lo"), int(
            getattr(sim_cfg, "DOMAIN_ID", 1)
        )
    except Exception:
        return cli_iface or "lo", 1


def build_lowcmd(q_targets: np.ndarray, leg_kp: float, leg_kd: float, crc: CRC) -> LowCmd_:
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
    for i in range(NUM_LEG):
        cmd.motor_cmd[i].q = float(q_targets[i])
        cmd.motor_cmd[i].kp = float(leg_kp)
        cmd.motor_cmd[i].dq = 0.0
        cmd.motor_cmd[i].kd = float(leg_kd)
        cmd.motor_cmd[i].tau = 0.0
    for j in range(NUM_ARM):
        i = NUM_LEG + j
        cmd.motor_cmd[i].q = 0.0
        cmd.motor_cmd[i].kp = 0.0
        cmd.motor_cmd[i].dq = 0.0
        cmd.motor_cmd[i].kd = 0.0
        cmd.motor_cmd[i].tau = float(q_targets[i])
    cmd.crc = crc.Crc(cmd)
    return cmd


class JointJoystickApp:
    def __init__(self, leg_kp: float, leg_kd: float, iface: str, domain_id: int):
        self.leg_kp = leg_kp
        self.leg_kd = leg_kd
        self.lock = threading.Lock()
        self.q = np.zeros(NUM_MOT, dtype=np.float64)
        self.low_state: LowState_ | None = None
        self.running = True
        self._suppress_scale_cb = False
        self.crc = CRC()

        ChannelFactoryInitialize(domain_id, iface)
        self.sub = ChannelSubscriber("rt/lowstate", LowState_)
        self.sub.Init(self._on_state, 10)
        self.pub = ChannelPublisher("rt/lowcmd", LowCmd_)
        self.pub.Init()

        self.root = tk.Tk()
        self.root.title("Go2 + Z1 — joystick tutti i giunti (18)")
        self.root.geometry("920x780")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        top = tk.Frame(self.root, padx=8, pady=6)
        top.pack(fill=tk.X)
        tk.Label(
            top,
            text="Muovi le barre (rad). Pubblicazione ~50 Hz. Prima avvia unitree_mujoco.py.",
            font=("", 10),
        ).pack(anchor="w")

        btnf = tk.Frame(self.root, padx=8, pady=4)
        btnf.pack(fill=tk.X)
        tk.Button(btnf, text="Sync da robot", command=self._sync_from_robot).pack(
            side=tk.LEFT, padx=4
        )
        tk.Button(btnf, text="Stand (gambe) + braccio ripiegato", command=self._stand_pose).pack(
            side=tk.LEFT, padx=4
        )
        tk.Button(btnf, text="Esci", command=self._on_close).pack(side=tk.RIGHT, padx=4)

        pan = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, sashwidth=6)
        pan.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        f_leg = tk.LabelFrame(pan, text="Gambe (12)", padx=6, pady=6)
        f_arm = tk.LabelFrame(pan, text="Braccio Z1 (6)", padx=6, pady=6)
        pan.add(f_leg, minsize=420)
        pan.add(f_arm, minsize=380)

        self._leg_scales: list[tk.Scale] = []
        self._leg_lbls: list[tk.Label] = []
        self._arm_scales: list[tk.Scale] = []
        self._arm_lbls: list[tk.Label] = []

        for i in range(NUM_LEG):
            lo, hi = LEG_LIMITS[i]
            row = tk.Frame(f_leg)
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=LEG_NAMES[i], width=11, anchor="w").pack(side=tk.LEFT)
            sc = tk.Scale(
                row,
                from_=lo,
                to=hi,
                resolution=0.005,
                orient=tk.HORIZONTAL,
                length=340,
                showvalue=0,
                command=lambda v, idx=i: self._on_leg_scale(idx, float(v)),
            )
            sc.pack(side=tk.LEFT, padx=4)
            vl = tk.Label(row, text="0.000", width=8, anchor="e")
            vl.pack(side=tk.LEFT)
            self._leg_scales.append(sc)
            self._leg_lbls.append(vl)

        for i in range(NUM_ARM):
            lo, hi = ARM_LIMITS[i]
            row = tk.Frame(f_arm)
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=ARM_NAMES[i], width=10, anchor="w").pack(side=tk.LEFT)
            sc = tk.Scale(
                row,
                from_=lo,
                to=hi,
                resolution=0.005,
                orient=tk.HORIZONTAL,
                length=300,
                showvalue=0,
                command=lambda v, idx=i: self._on_arm_scale(idx, float(v)),
            )
            sc.pack(side=tk.LEFT, padx=4)
            vl = tk.Label(row, text="0.000", width=8, anchor="e")
            vl.pack(side=tk.LEFT)
            self._arm_scales.append(sc)
            self._arm_lbls.append(vl)

        self._status = tk.Label(self.root, text="In attesa di lowstate…", fg="#555")
        self._status.pack(fill=tk.X, pady=4)

        self._pub_thread = threading.Thread(target=self._publish_loop, daemon=True)
        self._pub_thread.start()

        self.root.after(200, self._try_initial_sync)

    def _on_state(self, msg: LowState_):
        self.low_state = msg

    def _on_leg_scale(self, idx: int, val: float):
        if self._suppress_scale_cb:
            return
        with self.lock:
            self.q[idx] = val
        self._leg_lbls[idx].config(text=f"{val:.3f}")

    def _on_arm_scale(self, idx: int, val: float):
        if self._suppress_scale_cb:
            return
        with self.lock:
            self.q[NUM_LEG + idx] = val
        self._arm_lbls[idx].config(text=f"{val:.3f}")

    def _set_scales_from_q(self):
        self._suppress_scale_cb = True
        try:
            for i in range(NUM_LEG):
                v = float(self.q[i])
                self._leg_scales[i].set(v)
                self._leg_lbls[i].config(text=f"{v:.3f}")
            for j in range(NUM_ARM):
                v = float(self.q[NUM_LEG + j])
                self._arm_scales[j].set(v)
                self._arm_lbls[j].config(text=f"{v:.3f}")
        finally:
            self._suppress_scale_cb = False

    def _sync_from_robot(self):
        if self.low_state is None:
            self._status.config(text="Nessun lowstate ancora disponibile.")
            return
        with self.lock:
            for i in range(NUM_MOT):
                self.q[i] = float(self.low_state.motor_state[i].q)
        self._set_scales_from_q()
        self._status.config(text="Sincronizzato dalla posa attuale del simulatore.")

    def _stand_pose(self):
        with self.lock:
            self.q[:NUM_LEG] = STAND_LEG.copy()
            for j in range(NUM_ARM):
                self.q[NUM_LEG + j] = ARM_FOLD[j]
        self._set_scales_from_q()
        self._status.config(text="Target: stand policy + braccio ripiegato.")

    def _try_initial_sync(self):
        if self.low_state is not None:
            self._sync_from_robot()
            self._status.config(text="Pronto. Modifica i giunti o usa Sync / Stand.")
        else:
            self._status.config(text="In attesa di lowstate dal simulatore…")
            self.root.after(300, self._try_initial_sync)

    def _publish_loop(self):
        while self.running:
            if self.low_state is None:
                time.sleep(0.05)
                continue
            with self.lock:
                qt = self.q.copy()
            try:
                cmd = build_lowcmd(qt, self.leg_kp, self.leg_kd, self.crc)
                self.pub.Write(cmd)
            except Exception:
                pass
            time.sleep(0.02)

    def _on_close(self):
        self.running = False
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="Joystick 18 giunti Go2+Z1 (sim)")
    parser.add_argument("--interface", type=str, default=None, help="DDS, es. lo o lan2")
    parser.add_argument("--leg-kp", type=float, default=20.0, help="KP gambe (come ts_config ctrl_kp)")
    parser.add_argument("--leg-kd", type=float, default=0.5, help="KD gambe (come ts_config ctrl_kd)")
    args = parser.parse_args()

    iface, domain_id = _sim_interface(args.interface)
    print(f"DDS domain={domain_id} interface={iface}")
    print("In attesa di lowstate (avvia unitree_mujoco.py se non l’hai già fatto)…")

    app = JointJoystickApp(args.leg_kp, args.leg_kd, iface, domain_id)
    app.run()


if __name__ == "__main__":
    main()

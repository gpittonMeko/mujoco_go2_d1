#!/usr/bin/env python3
"""
Interfaccia virtuale per controllo braccio su Go2 (Unitree D1 mesh o Z1 menagerie).

Usa gli stessi comandi LowCmd del robot reale (q, kp, kd, dq, tau).
I limiti degli slider e i comandi sono allineati agli attributi `range` di
`arm_joint1`…`arm_joint6` nel MJCF scelto (default: go2_d1_d1mesh.xml = D1).

Uso:
  1. Avvia simulatore (unitree_mujoco_d1viz.py per mesh D1, oppure unitree_mujoco.py per Z1)
  2. python3 d1_arm/arm_control.py
     oppure: python3 d1_arm/arm_control.py --z1
     oppure: python3 d1_arm/arm_control.py --mjcf path/al/modello.xml

  UNITREE_ARM_MJCF=/path/to/go2_d1_d1mesh.xml  (opzionale, ha priorità su --mjcf assente)

  DDS: export UNITREE_SIM_INTERFACE=lo se il sim usa loopback (vedi config_d1viz).
"""

import argparse
import os
import re
import sys
import time
import threading
import math
import xml.etree.ElementTree as ET

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_sdk2_python"))

ROBOT_DIR = os.path.join(PROJECT_ROOT, "unitree_mujoco", "unitree_robots", "go2_d1")
D1_MESH_XML = os.path.join(ROBOT_DIR, "go2_d1_d1mesh.xml")
Z1_XML = os.path.join(ROBOT_DIR, "go2_d1.xml")

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.utils.crc import CRC

# Stand pose gambe (da go2_deploy ts_config)
STAND_POSE = [0.0, 0.8, -1.5, 0.0, 0.8, -1.5, 0.0, 0.8, -1.5, 0.0, 0.8, -1.5]

KP_LEGS = 20.0
KD_LEGS = 0.5

ARM_OFFSET = 12  # motor_cmd[12..17] = braccio

# Preset Z1 (go2_d1.xml) — radianti
_ARM_HOME_Z1 = [0.0, 0.785, -0.261, -0.523, 0.0, 0.0]
_ARM_FOLD_Z1 = [0.0, 0.2, -0.4, -0.2, 0.0, 0.0]

# Preset D1 mesh (keyframe home braccio in go2_d1_d1mesh.xml)
_ARM_HOME_D1 = [0.0, -1.5, 1.0, 0.22, 0.0, 0.0]
_ARM_FOLD_D1 = [0.0, -1.5, 1.0, 0.22, 0.0, 0.0]


def load_arm_joint_limits_rad(mjcf_path):
    """Legge range (rad) di arm_joint1..6 dal MJCF."""
    tree = ET.parse(mjcf_path)
    root = tree.getroot()
    found = {}
    for j in root.iter("joint"):
        name = j.get("name")
        if not name or not re.fullmatch(r"arm_joint[1-6]", name):
            continue
        r = j.get("range")
        if not r:
            continue
        parts = r.split()
        if len(parts) != 2:
            continue
        lo, hi = float(parts[0]), float(parts[1])
        idx = int(name.replace("arm_joint", "")) - 1
        found[idx] = (lo, hi)
    if len(found) != 6:
        raise ValueError(
            f"In {mjcf_path} servono arm_joint1..6 con attributo range; trovati: {sorted(found.keys())}"
        )
    lows = [found[i][0] for i in range(6)]
    highs = [found[i][1] for i in range(6)]
    return lows, highs


def clamp_joint_list(q, lows, highs):
    return [max(lows[i], min(highs[i], q[i])) for i in range(6)]


def resolve_mjcf_path(args):
    if args.mjcf:
        p = args.mjcf
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(PROJECT_ROOT, p))
    env_p = os.environ.get("UNITREE_ARM_MJCF", "").strip()
    if env_p:
        return env_p if os.path.isabs(env_p) else os.path.normpath(os.path.join(PROJECT_ROOT, env_p))
    if args.z1:
        return Z1_XML
    return D1_MESH_XML


def _init_dds():
    """Allinea Cyclone DDS al simulatore (stesso domain/interface di unitree_mujoco*.py)."""
    sim_py = os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python")
    if sim_py not in sys.path:
        sys.path.insert(0, sim_py)
    preferred = "lo"
    try:
        import config as sim_config
        preferred = getattr(sim_config, "INTERFACE", "lo")
    except ImportError:
        pass
    env_iface = os.environ.get("UNITREE_SIM_INTERFACE", "").strip()
    candidates = []
    if env_iface:
        candidates.append(env_iface)
    candidates.append(preferred)
    if "lo" not in candidates:
        candidates.append("lo")
    last_err = None
    for iface in candidates:
        try:
            ChannelFactoryInitialize(1, iface)
            print(f"DDS: domain_id=1, interface={iface}")
            return
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err


def main():
    parser = argparse.ArgumentParser(description="Slider braccio Go2 (limiti da MJCF)")
    parser.add_argument(
        "--mjcf",
        type=str,
        default=None,
        help="File MJCF con arm_joint1..6 (default: go2_d1_d1mesh.xml oppure go2_d1.xml con --z1)",
    )
    parser.add_argument(
        "--z1",
        action="store_true",
        help="Usa limiti e preset da go2_d1.xml (braccio Z1 menagerie)",
    )
    args = parser.parse_args()

    mjcf_path = resolve_mjcf_path(args)
    if not os.path.isfile(mjcf_path):
        print(f"MJCF non trovato: {mjcf_path}", file=sys.stderr)
        sys.exit(1)

    limits_lo, limits_hi = load_arm_joint_limits_rad(mjcf_path)
    _bn = os.path.basename(mjcf_path).lower()
    if _bn == "go2_d1_d1mesh.xml" or "d1mesh" in _bn:
        arm_home = list(_ARM_HOME_D1)
        arm_fold = list(_ARM_FOLD_D1)
        model_tag = "D1 mesh"
    else:
        arm_home = list(_ARM_HOME_Z1)
        arm_fold = list(_ARM_FOLD_Z1)
        model_tag = "Z1"

    arm_home = clamp_joint_list(arm_home, limits_lo, limits_hi)
    arm_fold = clamp_joint_list(arm_fold, limits_lo, limits_hi)

    print(f"Limiti braccio da: {mjcf_path}")
    print(f"Modello UI: {model_tag} (Home/Fold clampati al MJCF)")

    _init_dds()

    arm_pos = list(arm_home)
    low_state = {"msg": None}

    def on_state(msg: LowState_):
        low_state["msg"] = msg

    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(on_state, 10)
    pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    pub.Init()
    crc = CRC()

    def make_cmd(arm, crc_obj):
        arm_c = clamp_joint_list(arm, limits_lo, limits_hi)
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
            c.motor_cmd[ARM_OFFSET + i].dq = 0.0
            c.motor_cmd[ARM_OFFSET + i].kd = 0.0
            c.motor_cmd[ARM_OFFSET + i].tau = float(arm_c[i])
        c.crc = crc_obj.Crc(c)
        return c

    try:
        import tkinter as tk
    except ImportError:
        print("tkinter non disponibile. Uso pose Home fissa.")
        while True:
            cmd = make_cmd(arm_pos, crc)
            pub.Write(cmd)
            time.sleep(0.02)
        return

    def publish_loop():
        while True:
            cmd = make_cmd(arm_pos, crc)
            pub.Write(cmd)
            time.sleep(0.002)

    pub_thread = threading.Thread(target=publish_loop, daemon=True)
    pub_thread.start()

    root = tk.Tk()
    root.title(f"Arm control ({model_tag}) — limiti da {os.path.basename(mjcf_path)}")
    root.geometry("440x340")

    lbl_deg = [None] * 6
    sliders = []

    def deg_lo_hi(i):
        return math.degrees(limits_lo[i]), math.degrees(limits_hi[i])

    def on_slider(val_str, idx):
        lo_d, hi_d = deg_lo_hi(idx)
        v = max(lo_d, min(hi_d, float(val_str)))
        arm_pos[idx] = math.radians(v)
        # aggiorna slider se il valore è stato saturato
        sliders[idx].set(round(v))
        if lbl_deg[idx]:
            lbl_deg[idx]["text"] = f"{v:.1f}°"

    for i in range(6):
        lo_d, hi_d = deg_lo_hi(i)
        f = tk.Frame(root)
        f.pack(fill=tk.X, padx=8, pady=2)
        tk.Label(f, text=f"J{i+1}", width=3).pack(side=tk.LEFT)
        span = max(1.0, abs(hi_d - lo_d))
        res = 1.0 if span <= 180 else 2.0
        s = tk.Scale(
            f,
            from_=lo_d,
            to=hi_d,
            resolution=res,
            orient=tk.HORIZONTAL,
            length=300,
            command=lambda v, idx=i: on_slider(v, idx),
            showvalue=False,
        )
        cur_deg = math.degrees(arm_pos[i])
        s.set(round(max(lo_d, min(hi_d, cur_deg))))
        s.pack(side=tk.LEFT, fill=tk.X, expand=True)
        lbl = tk.Label(f, text=f"{cur_deg:.1f}°", width=7, font=("", 10))
        lbl.pack(side=tk.LEFT)
        lbl_deg[i] = lbl
        sliders.append(s)

    def set_pose(pose):
        pose_c = clamp_joint_list(pose, limits_lo, limits_hi)
        for i, v in enumerate(pose_c):
            arm_pos[i] = v
            lo_d, hi_d = deg_lo_hi(i)
            d = math.degrees(v)
            d = max(lo_d, min(hi_d, d))
            sliders[i].set(round(d))
            if lbl_deg[i]:
                lbl_deg[i]["text"] = f"{d:.1f}°"

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=8)
    tk.Button(btn_frame, text="Home", command=lambda: set_pose(arm_home)).pack(side=tk.LEFT, padx=4)
    tk.Button(btn_frame, text="Fold", command=lambda: set_pose(arm_fold)).pack(side=tk.LEFT, padx=4)

    def test_oscillate():
        """Oscilla J1 entro i limiti — verifica risposta."""
        orig = arm_pos[0]
        lo0, hi0 = limits_lo[0], limits_hi[0]
        amp = min(0.3, (hi0 - lo0) * 0.15)
        t0 = [time.perf_counter()]

        def _tick():
            elapsed = time.perf_counter() - t0[0]
            if elapsed < 3.0:
                q = orig + amp * math.sin(2 * math.pi * 0.5 * elapsed)
                q = max(lo0, min(hi0, q))
                arm_pos[0] = q
                sliders[0].set(round(math.degrees(q)))
                if lbl_deg[0]:
                    lbl_deg[0]["text"] = f"{math.degrees(q):.1f}°"
                root.after(20, _tick)
            else:
                arm_pos[0] = orig
                sliders[0].set(round(math.degrees(orig)))
                if lbl_deg[0]:
                    lbl_deg[0]["text"] = f"{math.degrees(orig):.1f}°"

        _tick()

    tk.Button(btn_frame, text="Test J1", command=test_oscillate).pack(side=tk.LEFT, padx=4)

    tk.Label(
        root,
        text="LowCmd: tau = q_des (rad), kp=kd=0 — slider limitati dal MJCF",
        fg="gray",
    ).pack(pady=4)

    root.mainloop()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Joystick/Viewer che mostra in tempo reale il movimento del braccio D1.
Legge rt/lowstate (motor_state[12..17]) e visualizza:
- I 6 angoli di giunto con barre
- Una vista 2D del braccio (piano X-Z, lato)
Avvia PRIMA il simulatore. Uso: python3 scripts/arm_joystick_viewer.py
"""
import math
import sys
import os
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_sdk2_python"))

from unitree_sdk2py.core.channel import ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_

try:
    import tkinter as tk
except ImportError:
    print("tkinter non disponibile. Installa: sudo apt install python3-tk")
    sys.exit(1)

# Geometria braccio Z1 (da run_go2_d1_ball)
L_UPPER = 0.35
L_FORE_X = 0.218
L_FORE_Z = 0.057
L_WRIST = 0.1447

J_LIMITS = [
    (-2.61799, 2.61799),   # J1
    (0.0, 2.96706),       # J2
    (-2.87979, 0.0),      # J3
    (-1.51844, 1.51844),  # J4
    (-1.3439, 1.3439),    # J5
    (-2.79253, 2.79253),  # J6
]
J_NAMES = ["J1 (base)", "J2 (shoulder)", "J3 (elbow)", "J4 (wrist1)", "J5 (wrist2)", "J6 (wrist3)"]


def arm_fk_points(j2, j3, j4):
    """Ritorna i punti (x,z) del braccio nel piano: shoulder, elbow, wrist, tip."""
    s23 = j2 + j3
    s234 = s23 + j4
    p0 = (0.0, 0.0)
    p1 = (-L_UPPER * math.cos(j2), L_UPPER * math.sin(j2))
    p2 = (
        p1[0] + L_FORE_X * math.cos(s23) + L_FORE_Z * math.sin(s23),
        p1[1] - L_FORE_X * math.sin(s23) + L_FORE_Z * math.cos(s23),
    )
    p3 = (
        p2[0] + L_WRIST * math.cos(s234),
        p2[1] - L_WRIST * math.sin(s234),
    )
    return [p0, p1, p2, p3]


class ArmViewerApp:
    def __init__(self):
        self.arm_joints = [0.0] * 6
        self.lock = threading.Lock()
        self.root = tk.Tk()
        self.root.title("Braccio D1 — Vista in tempo reale")
        self.root.geometry("520x420")
        self.root.resizable(True, True)

        # Frame barre giunti
        f_joints = tk.Frame(self.root, padx=10, pady=5)
        f_joints.pack(fill=tk.X)

        self.bars = []
        self.labels = []
        for i in range(6):
            row = tk.Frame(f_joints)
            row.pack(fill=tk.X, pady=2)
            lbl = tk.Label(row, text=J_NAMES[i], width=14, anchor="w")
            lbl.pack(side=tk.LEFT)
            canvas = tk.Canvas(row, height=18, width=280, bg="#333", highlightthickness=0)
            canvas.pack(side=tk.LEFT, padx=5)
            val_lbl = tk.Label(row, text="0.00 rad", width=10, anchor="e")
            val_lbl.pack(side=tk.LEFT)
            self.bars.append(canvas)
            self.labels.append(val_lbl)

        # Frame vista 2D
        f_canvas = tk.Frame(self.root, padx=10, pady=10)
        f_canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas_w = 400
        self.canvas_h = 280
        self.arm_canvas = tk.Canvas(
            f_canvas, width=self.canvas_w, height=self.canvas_h,
            bg="#1a1a2e", highlightthickness=1, highlightbackground="#444"
        )
        self.arm_canvas.pack(fill=tk.BOTH, expand=True)

        tk.Label(self.root, text="Vista laterale (X avanti, Z alto) — dati da rt/lowstate", font=("", 9), fg="#888").pack(pady=2)

    def norm_to_pct(self, val, lo, hi):
        if hi <= lo:
            return 0.5
        return (val - lo) / (hi - lo)

    def update_bars(self):
        with self.lock:
            joints = list(self.arm_joints)
        for i in range(6):
            lo, hi = J_LIMITS[i]
            pct = self.norm_to_pct(joints[i], lo, hi)
            pct = max(0, min(1, pct))
            w = 276
            fill_w = int(w * pct)
            self.bars[i].delete("all")
            self.bars[i].create_rectangle(2, 2, w + 2, 16, fill="#222", outline="")
            self.bars[i].create_rectangle(2, 2, 2 + fill_w, 16, fill="#4a9eff", outline="")
            self.labels[i].config(text=f"{joints[i]:+.2f} rad")

    def draw_arm(self):
        with self.lock:
            j2, j3, j4 = self.arm_joints[1], self.arm_joints[2], self.arm_joints[3]
        pts = arm_fk_points(j2, j3, j4)
        # Scala e centra nel canvas (x avanti = destra, z alto = alto)
        scale = 180
        cx, cy = 80, self.canvas_h - 40
        px = [cx + p[0] * scale for p in pts]
        pz = [cy - p[1] * scale for p in pts]

        self.arm_canvas.delete("all")
        # Griglia
        for i in range(0, self.canvas_w, 40):
            self.arm_canvas.create_line(i, 0, i, self.canvas_h, fill="#2a2a3e", width=1)
        for j in range(0, self.canvas_h, 40):
            self.arm_canvas.create_line(0, j, self.canvas_w, j, fill="#2a2a3e", width=1)
        # Braccio
        for k in range(len(px) - 1):
            self.arm_canvas.create_line(px[k], pz[k], px[k + 1], pz[k + 1], fill="#4a9eff", width=4)
        for k in range(len(px)):
            r = 6 if k < len(px) - 1 else 8
            fill = "#6bb3ff" if k < len(px) - 1 else "#ffd54a"
            self.arm_canvas.create_oval(px[k] - r, pz[k] - r, px[k] + r, pz[k] + r, fill=fill, outline="#fff", width=2)
        self.arm_canvas.create_text(cx - 50, cy, text="base", fill="#888", font=("", 9))

    def refresh(self):
        self.update_bars()
        self.draw_arm()
        self.root.after(50, self.refresh)

    def state_callback(self, msg):
        with self.lock:
            for i in range(6):
                self.arm_joints[i] = msg.motor_state[12 + i].q

    def run(self):
        self.root.after(100, self.refresh)
        self.root.mainloop()


def main():
    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python"))
        import config as sim_config
        iface = sim_config.INTERFACE
    except ImportError:
        iface = "lo"

    print("Braccio D1 — Viewer in tempo reale")
    print("  Avvia PRIMA il simulatore: cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py")
    print(f"  Domain=1, interface={iface}")
    print("  Chiudi la finestra per uscire.")
    ChannelFactoryInitialize(1, iface)

    app = ArmViewerApp()
    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(app.state_callback, 10)
    app.run()


if __name__ == "__main__":
    main()

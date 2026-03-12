#!/usr/bin/env python3
"""
Autonomous ball approach: Go2 d1+Z1 detects red ball, walks toward it,
reaches with the arm using active searching, and grabs it magnetically.

States: STAND → WALK (bypass search) → REACH → GRABBED
"""

import time, sys, os, math, threading, json, socket, argparse, signal, select
import numpy as np

_stop_requested = False
def _on_stop_signal(signum, frame):
    global _stop_requested
    _stop_requested = True

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_sdk2_python"))
sys.path.insert(0, PROJECT_ROOT)

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "deploy_policy", os.path.join(PROJECT_ROOT, "scripts", "deploy_policy.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
PolicyRunner = _mod.PolicyRunner

from unitree_sdk2py.core.channel import (ChannelPublisher, ChannelSubscriber,
                                          ChannelFactoryInitialize)
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.utils.crc import CRC

# ── Arm geometry (Z1 on Go2) ─────────────────────────────────────────
ARM_BASE_X = 0.15
ARM_J2_Z   = 0.06 + 0.0585 + 0.045
L_UPPER    = 0.35
L_FORE_X   = 0.218
L_FORE_Z   = 0.057
L_WRIST    = 0.1447

ARM_FOLD = [0.0, 0.2, -0.4, -0.2, 0.0, 0.0]

J1_LIM = (-2.61799, 2.61799)
J2_LIM = (0.0, 2.96706)
J3_LIM = (-2.87979, 0.0)
J4_LIM = (-1.51844, 1.51844)
J5_LIM = (-1.3439, 1.3439)
J6_LIM = (-2.79253, 2.79253)

# ── Arm search: braccio piegato, solo polso (J5/J6) guarda intorno ──
SEARCH_POSES = [
    [ 0.0,  0.2, -0.4, -0.2,  0.0,  0.0],   # centro
    [ 0.0,  0.2, -0.4, -0.2,  1.2,  0.0],   # destra
    [ 0.0,  0.2, -0.4, -0.2,  1.2,  0.25],  # destra-basso
    [ 0.0,  0.2, -0.4, -0.2,  1.2,  0.0],   # destra
    [ 0.0,  0.2, -0.4, -0.2,  0.0,  0.0],   # centro
    [ 0.0,  0.2, -0.4, -0.2, -1.2,  0.0],   # sinistra
    [ 0.0,  0.2, -0.4, -0.2, -1.2,  0.25],  # sinistra-basso
    [ 0.0,  0.2, -0.4, -0.2, -1.2,  0.0],   # sinistra
    [ 0.0,  0.2, -0.4, -0.2,  0.0,  0.0],   # centro
    [ 0.0,  0.2, -0.4, -0.2,  0.9, -0.35],  # destra-alto
    [ 0.0,  0.2, -0.4, -0.2, -0.9, -0.35],  # sinistra-alto
    [ 0.0,  0.2, -0.4, -0.2,  0.0,  0.0],   # centro
]
SEARCH_POSE_TIME = 2.5

CROUCH_OFFSETS = {1: 0.15, 4: 0.15, 7: 0.15, 10: 0.15,
                  2: -0.25, 5: -0.25, 8: -0.25, 11: -0.25}
REACH_CROUCH_OFFSETS = {1: 0.22, 4: 0.22, 7: 0.22, 10: 0.22,
                        2: -0.35, 5: -0.35, 8: -0.35, 11: -0.35}
MAX_ARM_REACH_FWD = 0.45
REACH_SETTLE_TIME = 2.5
REACH_ALPHA = 0.0002
MAX_JOINT_STEP = 0.0005
REACH_DIST_LO = 0.77
REACH_DIST_HI = 0.90
REACH_SLOW_ZONE = 0.83
VX_COMPENSATE_BACK = -0.085

# ── States ────────────────────────────────────────────────────────────
STAND, ARM_SEARCH, WRIST_ALIGN, WALK, REACH, GRABBED, FALLEN = range(7)
STATE_NAMES = ["STAND", "ARM_SEARCH", "WRIST_ALIGN", "WALK", "REACH", "GRABBED", "FALLEN"]

FALL_Z_THRESH = 0.15
RECOVERY_Z_THRESH = 0.25
FALL_CONFIRM_FRAMES = 15

UDP_PORT = 9870
PRINT_DT = 0.8


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def apply_wrist_centering(arm_target, w_center_delta):
    """Applica la correzione J5/J6 calcolata dalla visione nel simulatore.
    w_center_delta viene dal simulatore: render camera → OpenCV detect → delta per centrare."""
    if len(arm_target) < 6 or len(w_center_delta) < 2:
        return
    arm_target[4] = clamp(arm_target[4] + w_center_delta[0], *J5_LIM)
    arm_target[5] = clamp(arm_target[5] + w_center_delta[1], *J6_LIM)


def fk_plane(j2, j3, j4):
    s23 = j2 + j3
    s234 = s23 + j4
    x = (-L_UPPER * math.cos(j2)
         + L_FORE_X * math.cos(s23) + L_FORE_Z * math.sin(s23)
         + L_WRIST * math.cos(s234))
    z = (L_UPPER * math.sin(j2)
         - L_FORE_X * math.sin(s23) + L_FORE_Z * math.cos(s23)
         - L_WRIST * math.sin(s234))
    return x, z


def ik_arm(target_x, target_y, target_z):
    """Numerical IK. target_x/y/z are in robot base_link frame."""
    dx = target_x - ARM_BASE_X
    dy = target_y
    j1 = clamp(math.atan2(dy, max(dx, 0.05)), *J1_LIM)
    r_t = math.sqrt(dx**2 + dy**2)
    z_t = target_z - ARM_J2_Z

    best, best_err = None, 1e9
    for j2i in [1.5, 2.0, 2.5, 2.9]:
        for j3i in [-0.5, -1.0, -1.5, -2.0]:
            for j4i in [-0.5, 0.0, 0.5, 1.0]:
                j2, j3, j4 = j2i, j3i, j4i
                lr = 0.8
                for _ in range(120):
                    x, z = fk_plane(j2, j3, j4)
                    ex, ez = x - r_t, z - z_t
                    err = ex*ex + ez*ez
                    if err < 0.0002: break
                    eps = 1e-4
                    dx2 = (fk_plane(j2+eps, j3, j4)[0] - x)/eps
                    dz2 = (fk_plane(j2+eps, j3, j4)[1] - z)/eps
                    dx3 = (fk_plane(j2, j3+eps, j4)[0] - x)/eps
                    dz3 = (fk_plane(j2, j3+eps, j4)[1] - z)/eps
                    dx4 = (fk_plane(j2, j3, j4+eps)[0] - x)/eps
                    dz4 = (fk_plane(j2, j3, j4+eps)[1] - z)/eps
                    j2 -= lr*(ex*dx2 + ez*dz2)
                    j3 -= lr*(ex*dx3 + ez*dz3)
                    j4 -= lr*(ex*dx4 + ez*dz4)
                    j2 = clamp(j2, *J2_LIM)
                    j3 = clamp(j3, *J3_LIM)
                    j4 = clamp(j4, *J4_LIM)
                    lr *= 0.995
                x, z = fk_plane(j2, j3, j4)
                e = (x-r_t)**2 + (z-z_t)**2
                if e < best_err:
                    best_err = e; best = (j2, j3, j4)

    if best is None or best_err > 0.01:
        return None
    j2, j3, j4 = best[0], best[1], best[2]
    j2 = clamp(j2, 0.0, 2.35)
    return [j1, j2, j3, j4, 0.0, 0.0]


def smooth(cur, tgt, alpha=0.05):
    return [c + alpha*(t - c) for c, t in zip(cur, tgt)]


def step_toward(current, target, max_step):
    """Limita lo spostamento per step (traiettoria multi-step)."""
    out = []
    for c, t in zip(current, target):
        diff = t - c
        step = clamp(diff, -max_step, max_step)
        out.append(c + step)
    return out


# ── UDP receiver ──────────────────────────────────────────────────────
class BallReceiver:
    def __init__(self):
        self.detected = False
        self.pos = [0.0, 0.0, 0.0]
        self.robot_z = 0.33
        self.wrist_detected = False
        self.wrist_depth = -1.0
        self.wrist_pixel = [0.0, 0.0]
        self.wrist_center_delta = [0.0, 0.0]
        self.grabbed = False
        self.grab_dist = 999.0
        self.lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", UDP_PORT))
        self._sock.settimeout(0.5)
        self._stop = threading.Event()
        self._timeout_count = 0
        self.SIM_GONE_THRESH = 10

    def simulator_gone(self):
        with self.lock:
            return self._timeout_count >= self.SIM_GONE_THRESH

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(2048)
                m = json.loads(data)
                with self.lock:
                    self._timeout_count = 0
                    self.detected = m.get("detected", False)
                    if self.detected:
                        self.pos = m["pos"]
                    self.robot_z = m.get("robot_z", 0.33)
                    self.wrist_detected = m.get("wrist_detected", False)
                    self.wrist_depth = m.get("wrist_depth", -1.0)
                    self.wrist_pixel = m.get("wrist_pixel", [0.0, 0.0])
                    self.wrist_center_delta = m.get("wrist_center_delta", [0.0, 0.0])
                    self.grabbed = m.get("grabbed", False)
                    self.grab_dist = m.get("grab_dist", 999.0)
            except socket.timeout:
                with self.lock:
                    self._timeout_count = min(self.SIM_GONE_THRESH, self._timeout_count + 1)
                    self.detected = False
                    self.wrist_detected = False
            except Exception:
                pass

    def get(self):
        with self.lock:
            return (self.detected, list(self.pos), self.robot_z,
                    self.wrist_detected, self.wrist_depth, list(self.wrist_pixel),
                    list(self.wrist_center_delta), self.grabbed, self.grab_dist)

    def stop(self):
        self._stop.set()


# ── Main ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="ts", choices=["ts", "wtw"])
    parser.add_argument("--interface", type=str, default=None)
    args = parser.parse_args()

    models_dir = os.path.join(PROJECT_ROOT, "go2_deploy", "models")
    params_dir = os.path.join(PROJECT_ROOT, "go2_deploy", "params")
    if args.model == "ts":
        cfg_path = os.path.join(params_dir, "ts_config.yaml")
        mdl_path = os.path.join(models_dir, "policy_ts_gs.pt")
    else:
        cfg_path = os.path.join(params_dir, "wtw_config.yaml")
        mdl_path = os.path.join(models_dir, "wtw_model.pt")

    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python"))
        import config as sim_cfg
        iface = args.interface or sim_cfg.INTERFACE
    except ImportError:
        iface = args.interface or "lo"
    ChannelFactoryInitialize(1, iface)

    runner = PolicyRunner(cfg_path, mdl_path, 0.0, 0.0, 0.0, arm_hold=True)
    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(runner.state_callback, 10)
    pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    pub.Init()
    crc = CRC()

    rx = BallReceiver()
    rx.start()

    signal.signal(signal.SIGUSR1, _on_stop_signal)
    signal.signal(signal.SIGUSR2, _on_stop_signal)
    print("=== Go2 d1+Z1 — Autonomous Ball Approach ===")
    print("  Per uscire: premi 'q'+Invio, Ctrl+C, oppure: kill -USR1 $(pgrep -f run_go2_d1_ball)")
    print("Waiting for lowstate...")
    while runner.low_state is None:
        time.sleep(0.1)
    print("Stabilizing...")
    time.sleep(2.0)

    state = STAND
    arm_cmd = list(ARM_FOLD)
    arm_target = list(ARM_FOLD)
    t_enter = time.time()
    t_print = 0.0
    search_idx = 0
    search_pose_t = 0.0
    crouch = False
    body_lost_t = None
    fall_low_count = 0

    try:
        while True:
            if _stop_requested:
                print("\nUscita richiesta (SIGUSR1/2).")
                break
            if rx.simulator_gone():
                print("\nSimulatore chiuso. Uscita.")
                break
            if sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]:
                c = sys.stdin.read(1)
                if c and c.lower() == 'q':
                    print("\nUscita (tasto 'q').")
                    break
            now = time.time()
            t0 = time.perf_counter()
            do_print = (now - t_print) >= PRINT_DT

            (detected, bpos, robot_z, w_det, w_depth, w_pix,
             w_center_delta, grabbed, grab_dist) = rx.get()
            bx, by, bz = bpos
            dist = math.sqrt(bx**2 + by**2)
            angle = math.atan2(by, bx)

            vx = vy = vyaw = 0.0

            walking = False

            # ── FALLEN: cane caduto (conferma su N frame per evitare falsi positivi) ──
            if robot_z < FALL_Z_THRESH:
                fall_low_count += 1
                if fall_low_count >= FALL_CONFIRM_FRAMES and state != FALLEN:
                    state = FALLEN
                    t_enter = now
                    arm_target = list(ARM_FOLD)
                    print("[FALLEN] Caduto! Chiudo braccio e provo a rialzarmi...")
                if state == FALLEN:
                    arm_target = list(ARM_FOLD)
                    crouch = False
            else:
                fall_low_count = 0
                if state == FALLEN:
                    if robot_z > RECOVERY_Z_THRESH:
                        state = STAND
                        t_enter = now
                        print("[STAND] Rialzato! Riprendo...")
                    else:
                        arm_target = list(ARM_FOLD)
                        crouch = False

            # ── GRABBED: ball caught! ──
            if grabbed and state != GRABBED:
                state = GRABBED
                t_enter = now
                crouch = False
                print("\n" + "="*50)
                print("   *** PALLA PRESA! ***")
                print("="*50 + "\n")

            # ── State machine ──
            if state == STAND:
                crouch = False
                if now - t_enter > 3.0:
                    state = WALK
                    t_enter = now
                    arm_target = list(ARM_FOLD)
                    print("[WALK] Bypass search, cammino verso palla...")

            elif state == ARM_SEARCH:
                crouch = True

                if now - search_pose_t > SEARCH_POSE_TIME:
                    search_idx = (search_idx + 1) % len(SEARCH_POSES)
                    search_pose_t = now
                arm_target = list(SEARCH_POSES[search_idx])
                if w_det:
                    apply_wrist_centering(arm_target, w_center_delta)
                    px = w_pix[0]
                    if abs(px) > 0.25:
                        state = WRIST_ALIGN
                        t_enter = now
                        crouch = False
                        arm_target = list(ARM_FOLD)
                        print(f"[WRIST_ALIGN] Palla a {'destra' if px > 0 else 'sinistra'}, allineo cane...")
                    else:
                        state = REACH
                        t_enter = now
                        crouch = False
                        print(f"[REACH] Wrist vede palla centrata! dist={dist:.2f}m")
                elif detected and dist > 0.90:
                    state = WALK
                    t_enter = now
                    crouch = False
                    arm_target = list(ARM_FOLD)
                    print(f"[WALK] Palla lontana ({dist:.2f}m), cammino...")
                elif detected and dist <= REACH_DIST_HI:
                    if do_print:
                        print(f"[ARM_SEARCH] Palla in range ({dist:.2f}m), cerco con wrist...")
                        t_print = now
                elif do_print:
                    print(f"[ARM_SEARCH] pose {search_idx}/{len(SEARCH_POSES)}")
                    t_print = now

                if now - t_enter > len(SEARCH_POSES) * SEARCH_POSE_TIME:
                    vyaw = 0.25
                    if do_print:
                        print("[ARM_SEARCH] Giro su me stesso per cercare...")
                        t_print = now

            elif state == WRIST_ALIGN:
                crouch = False
                arm_target = list(ARM_FOLD)
                if w_det:
                    apply_wrist_centering(arm_target, w_center_delta)

                if w_det:
                    px = w_pix[0]
                    vyaw = clamp(0.4 * px, -0.4, 0.4)
                    if do_print:
                        print(f"[WRIST_ALIGN] px={px:+.2f} vyaw={vyaw:+.2f} allineo...")
                        t_print = now

                    if abs(px) < 0.15 or (now - t_enter) > 2.0:
                        if dist > REACH_DIST_HI:
                            state = WALK
                            t_enter = now
                            arm_target = list(ARM_FOLD)
                            print(f"[WALK] Allineato! Cammino verso palla ({dist:.2f}m)")
                        else:
                            state = REACH
                            t_enter = now
                            print(f"[REACH] Allineato! dist={dist:.2f}m, estendo braccio")
                else:
                    if now - t_enter > 3.0:
                        state = WALK
                        t_enter = now
                        print("[WALK] Persa in ALIGN, cerco...")

            elif state == WALK:
                crouch = False
                walking = True
                arm_target = list(ARM_FOLD)

                if not detected:
                    vyaw = 0.25
                    if do_print:
                        print("[WALK] Palla persa, giro per cercare...")
                        t_print = now
                else:
                    body_lost_t = None
                    vyaw = clamp(0.8 * angle, -0.5, 0.5)

                    if dist > REACH_DIST_HI:
                        vx = clamp(0.25 * dist, 0.06, 0.18)
                        if do_print:
                            print(f"[WALK] dist={dist:.2f}m vx={vx:.2f} vyaw={vyaw:+.2f}")
                            t_print = now
                    else:
                        state = REACH
                        t_enter = now
                        print(f"[REACH] Vicino ({dist:.2f}m)! Estendo braccio...")

            elif state == REACH:
                crouch = True

                if dist > REACH_DIST_HI:
                    vx = 0.02
                    vyaw = clamp(0.08 * angle, -0.08, 0.08)
                    walking = True
                elif dist > REACH_SLOW_ZONE:
                    vx = 0.008
                    vyaw = clamp(0.05 * angle, -0.05, 0.05)
                    walking = True
                elif dist < REACH_DIST_LO:
                    vx = -0.04
                    vyaw = clamp(0.08 * angle, -0.08, 0.08)
                    walking = True
                else:
                    vx = vyaw = 0.0
                    walking = False

                if walking:
                    arm_target = list(ARM_FOLD)
                elif (now - t_enter) < REACH_SETTLE_TIME:
                    arm_target = list(ARM_FOLD)
                    if do_print:
                        print(f"[REACH] Stabilizzazione {now - t_enter:.1f}s...")
                        t_print = now
                else:
                    tx, ty, tz = bx, by, bz
                    if dist > MAX_ARM_REACH_FWD and dist > 0.01:
                        scale = MAX_ARM_REACH_FWD / dist
                        tx, ty = bx * scale, by * scale
                        tz = bz * 0.97 + 0.01
                    ik_res = ik_arm(tx, ty, tz)
                    if ik_res is not None:
                        arm_target = step_toward(arm_cmd, ik_res, MAX_JOINT_STEP)
                        if w_det and w_depth > 0:
                            px, py = w_pix
                            arm_target[0] += 0.04 * px
                            arm_target[2] -= 0.03 * py
                            arm_target[0] = clamp(arm_target[0], *J1_LIM)
                            arm_target[2] = clamp(arm_target[2], *J3_LIM)
                            apply_wrist_centering(arm_target, w_center_delta)
                        if do_print:
                            js = " ".join(f"{j:+.2f}" for j in arm_target[:4])
                            print(f"[REACH] d={dist:.2f} fermo, braccio lento verso palla [{js}]")
                            t_print = now
                    else:
                        if dist > REACH_DIST_LO:
                            vx = 0.03
                            walking = True
                            arm_target = list(ARM_FOLD)
                        if do_print:
                            print(f"[REACH] d={dist:.2f} fuori portata, avvicino")
                            t_print = now

                if not detected and not w_det:
                    if body_lost_t is None:
                        body_lost_t = now
                    elif now - body_lost_t > 4.0:
                        state = WALK
                        t_enter = now
                        body_lost_t = None
                        arm_target = list(ARM_FOLD)
                        print("[WALK] Persa in REACH, cerco con body cam...")
                else:
                    body_lost_t = None

            elif state == GRABBED:
                crouch = False
                if w_det:
                    apply_wrist_centering(arm_target, w_center_delta)
                if do_print:
                    print("[GRABBED] Palla presa! Mantengo posizione.")
                    t_print = now

            # ── Smooth arm (più lento in REACH per non sbilanciare) ──
            if state == FALLEN:
                a = 0.15
            elif walking:
                a = 0.02
            elif state == REACH and not walking:
                a = REACH_ALPHA
            elif state == ARM_SEARCH:
                a = 0.025
            elif crouch:
                a = 0.04
            else:
                a = 0.08
            arm_cmd = smooth(arm_cmd, arm_target, a)

            # ── Leg commands with optional crouch ──
            runner.cmd[0] = vx
            runner.cmd[1] = vy
            runner.cmd[2] = vyaw
            target_pos = runner.step()

            if crouch:
                tp = list(target_pos) if not isinstance(target_pos, list) else target_pos
                offs = REACH_CROUCH_OFFSETS if state == REACH else CROUCH_OFFSETS
                for idx, off in offs.items():
                    if idx < len(tp):
                        tp[idx] += off
                target_pos = tp

            cmd = runner.make_cmd(target_pos)
            for i in range(6):
                cmd.motor_cmd[12 + i].q = 0.0
                cmd.motor_cmd[12 + i].kp = 0.0
                cmd.motor_cmd[12 + i].dq = 0.0
                cmd.motor_cmd[12 + i].kd = 0.0
                cmd.motor_cmd[12 + i].tau = float(arm_cmd[i])
            cmd.crc = crc.Crc(cmd)
            pub.Write(cmd)

            elapsed = time.perf_counter() - t0
            if runner.dt - elapsed > 0:
                time.sleep(runner.dt - elapsed)

    except KeyboardInterrupt:
        print("\nFermato.")
    finally:
        rx.stop()


if __name__ == "__main__":
    main()

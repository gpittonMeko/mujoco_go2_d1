#!/usr/bin/env python3
"""
Go2 + braccio Z1 (MJCF `go2_d1.xml`): palla rossa, cammino, reach, presa magnetica.

Cinematica: `arm_kinematics.py`. Sim: `unitree_mujoco.py` + `config.py`.

Per la variante mesh D1 (`go2_d1_d1mesh.xml`) usare **solo** l’altro entry point
`run_go2_d1_ball_d1kin.py` (non modificare questo file per il D1).

States: STAND → WALK → REACH → GRABBED
"""

import time, sys, os, math, threading, json, socket, argparse, signal, select
import numpy as np

# Impostato unicamente da `run_go2_d1_ball_d1kin.py` prima di eseguire questo script.
_GO2_BALL_D1_PROFILE = sys.modules.get("go2_ball_d1_profile")

from arm_kinematics import ik_reach, smooth, J_LIMITS, ARM_BASE_Z

if _GO2_BALL_D1_PROFILE is not None:
    ARM_FOLD = list(_GO2_BALL_D1_PROFILE.ARM_FOLD)
    ARM_REACH_FWD = list(_GO2_BALL_D1_PROFILE.ARM_REACH_FWD)
    SEARCH_POSES = list(_GO2_BALL_D1_PROFILE.SEARCH_POSES)
else:
    ARM_FOLD = [0.0, 0.2, -0.4, -0.2, 0.0, 0.0]
    ARM_REACH_FWD = [0.0, 0.42, -0.7, 0.1, 0.0, -0.785]
    SEARCH_POSES = [
        [0.0, 0.2, -0.4, -0.2, 0.0, 0.0],
        [0.0, 0.2, -0.4, -0.2, 1.2, 0.0],
        [0.0, 0.2, -0.4, -0.2, 1.2, 0.25],
        [0.0, 0.2, -0.4, -0.2, 1.2, 0.0],
        [0.0, 0.2, -0.4, -0.2, 0.0, 0.0],
        [0.0, 0.2, -0.4, -0.2, -1.2, 0.0],
        [0.0, 0.2, -0.4, -0.2, -1.2, 0.25],
        [0.0, 0.2, -0.4, -0.2, -1.2, 0.0],
        [0.0, 0.2, -0.4, -0.2, 0.0, 0.0],
        [0.0, 0.2, -0.4, -0.2, 0.9, -0.35],
        [0.0, 0.2, -0.4, -0.2, -0.9, -0.35],
        [0.0, 0.2, -0.4, -0.2, 0.0, 0.0],
    ]

_stop_requested = False
def _on_stop_signal(signum, frame):
    global _stop_requested
    _stop_requested = True

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_sdk2_python"))
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, PROJECT_ROOT)

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "deploy_policy", os.path.join(PROJECT_ROOT, "scripts", "deploy_policy.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
PolicyRunner = _mod.PolicyRunner
LEG_TRIM_GO2_D1 = getattr(_mod, "LEG_TRIM_GO2_D1", None)

from unitree_sdk2py.core.channel import (ChannelPublisher, ChannelSubscriber,
                                          ChannelFactoryInitialize)
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.utils.crc import CRC

SEARCH_POSE_TIME = 2.5

CROUCH_OFFSETS = {1: 0.15, 4: 0.15, 7: 0.15, 10: 0.15,
                  2: -0.25, 5: -0.25, 8: -0.25, 11: -0.25}
REACH_CROUCH_OFFSETS = {1: 0.22, 4: 0.22, 7: 0.22, 10: 0.22,
                        2: -0.35, 5: -0.35, 8: -0.35, 11: -0.35}
MAX_ARM_REACH_FWD = 0.45
REACH_SETTLE_TIME = 2.5
REACH_DIST_LO = 0.52   # troppo vicino (senza wrist): indietreggia
REACH_DIST_HI = 0.65   # entra in REACH (braccio esteso)
REACH_SLOW_ZONE = 0.58 # rallenta in avvicinamento
TOUCH_DIST = 0.15      # quando wrist vede palla: avvicina fino a toccare
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
    """Correzione J5/J6 dalla visione (wrist camera) per centrare la palla."""
    if len(arm_target) < 6 or len(w_center_delta) < 2:
        return
    arm_target[4] = clamp(arm_target[4] + w_center_delta[0], *J_LIMITS[4])
    arm_target[5] = clamp(arm_target[5] + w_center_delta[1], *J_LIMITS[5])


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

    # arm_hold=True applica LEG_OFFSET_GO2_Z1 in deploy_policy (solo per Z1). Con profilo D1 il braccio
    # è comandato qui sotto in tau e l'offset Z1 falserebbe la stance → instabilità / cadute.
    _z1_leg_trim = _GO2_BALL_D1_PROFILE is None
    runner = PolicyRunner(
        cfg_path,
        mdl_path,
        0.0,
        0.0,
        0.0,
        arm_hold=_z1_leg_trim,
        leg_ctrl_kp=26.0 if _GO2_BALL_D1_PROFILE is not None else None,
        leg_ctrl_kd=0.62 if _GO2_BALL_D1_PROFILE is not None else None,
        leg_trim=(
            LEG_TRIM_GO2_D1
            if _GO2_BALL_D1_PROFILE is not None and LEG_TRIM_GO2_D1 is not None
            else None
        ),
    )
    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(runner.state_callback, 10)
    pub = ChannelPublisher("rt/lowcmd", LowCmd_)
    pub.Init()
    crc = CRC()

    rx = BallReceiver()
    rx.start()

    signal.signal(signal.SIGUSR1, _on_stop_signal)
    signal.signal(signal.SIGUSR2, _on_stop_signal)
    print(
        "=== Go2 + braccio — Autonomous Ball Approach ==="
        + (" (D1 mesh, PD gambe rinforzate)" if _GO2_BALL_D1_PROFILE is not None else " (Z1, offset gambe)")
    )
    print("  Per uscire: premi 'q'+Invio, Ctrl+C, oppure: kill -USR1 $(pgrep -f run_go2_d1_ball)")
    print("Waiting for lowstate...")
    while runner.low_state is None:
        time.sleep(0.1)
    print("Stabilizing...")
    time.sleep(2.0)

    state = STAND
    if (
        _GO2_BALL_D1_PROFILE is not None
        and getattr(_GO2_BALL_D1_PROFILE, "SYNC_ARM_FROM_LOWSTATE", False)
    ):
        ls0 = runner.low_state
        arm_cmd = [float(ls0.motor_state[12 + i].q) for i in range(6)]
        arm_target = list(arm_cmd)
    else:
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
                elif detected and dist > REACH_DIST_HI:
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
                arm_target = list(ARM_REACH_FWD)

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
                crouch = False

                # Wrist vede palla: avvicina fino a toccarla (TOUCH_DIST)
                if w_det:
                    if dist > TOUCH_DIST:
                        vx = clamp(0.12 * (dist - TOUCH_DIST), 0.02, 0.12)
                        vyaw = clamp(0.08 * angle, -0.08, 0.08)
                        walking = True
                        if do_print:
                            print(f"[REACH] Wrist vede palla, avvicino (d={dist:.2f}m -> {TOUCH_DIST}m)")
                            t_print = now
                    else:
                        vx = vyaw = 0.0
                        walking = False
                elif dist > REACH_DIST_HI:
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

                # Wrist vede palla: braccio va verso la palla (IK) anche mentre cammina
                tx, ty, tz = bx, by, bz
                if dist > MAX_ARM_REACH_FWD and dist > 0.01:
                    scale = MAX_ARM_REACH_FWD / dist
                    tx, ty = bx * scale, by * scale
                    tz = bz * 0.97 + 0.01
                tz = min(tz, ARM_BASE_Z + 0.22)
                tz = max(tz, 0.08)
                ik_res = ik_reach(tx, ty, tz)

                if w_det and ik_res is not None:
                    # Wrist vede: braccio si avvicina alla palla (anche quando robot cammina)
                    arm_target = list(ik_res)
                    if w_depth > 0:
                        px, py = w_pix
                        arm_target[0] += 0.04 * px
                        arm_target[2] -= 0.03 * py
                        arm_target[0] = clamp(arm_target[0], *J_LIMITS[0])
                        arm_target[2] = clamp(arm_target[2], *J_LIMITS[2])
                    apply_wrist_centering(arm_target, w_center_delta)
                    if do_print:
                        js = " ".join(f"{j:+.2f}" for j in arm_target[:4])
                        print(f"[REACH] Wrist vede palla, braccio verso [{js}]")
                        t_print = now
                elif walking:
                    arm_target = list(ARM_REACH_FWD)
                elif (now - t_enter) < REACH_SETTLE_TIME:
                    arm_target = list(ARM_REACH_FWD)
                    if do_print:
                        print(f"[REACH] Stabilizzazione {now - t_enter:.1f}s...")
                        t_print = now
                elif ik_res is not None:
                    arm_target = list(ik_res)
                    if w_det and w_depth > 0:
                        px, py = w_pix
                        arm_target[0] += 0.04 * px
                        arm_target[2] -= 0.03 * py
                        arm_target[0] = clamp(arm_target[0], *J_LIMITS[0])
                        arm_target[2] = clamp(arm_target[2], *J_LIMITS[2])
                    apply_wrist_centering(arm_target, w_center_delta)
                    if do_print:
                        js = " ".join(f"{j:+.2f}" for j in arm_target[:4])
                        print(f"[REACH] d={dist:.2f} fermo, braccio verso palla [{js}]")
                        t_print = now
                else:
                    if dist > REACH_DIST_LO:
                        vx = 0.03
                        walking = True
                        arm_target = list(ARM_FOLD)
                    else:
                        arm_target = list(ARM_REACH_FWD)
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

            # ── Smooth arm (come af140dc: transizioni graduali anche con profilo D1) ──
            if state == FALLEN:
                a = 0.15
            elif walking:
                a = 0.02
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

            if crouch and state != REACH:
                tp = list(target_pos) if not isinstance(target_pos, list) else target_pos
                for idx, off in CROUCH_OFFSETS.items():
                    if idx < len(tp):
                        tp[idx] += off
                target_pos = tp

            cmd = runner.make_cmd(target_pos)
            # Braccio (go2_d1.xml Z1 o go2_d1_d1mesh D1): attuatori <general>, ctrl = tau (setpoint q, rad)
            # come in d1_arm/arm_control.py — non usare kp/kd su q qui.
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

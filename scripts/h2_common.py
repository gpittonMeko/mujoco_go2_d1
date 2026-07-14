"""Shared helpers for Unitree H2 demo on Jetson (gentle arm motion)."""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

# Repo root: parent of scripts/
REPO_ROOT = Path(__file__).resolve().parent.parent
SDK_DIR = REPO_ROOT / "unitree_sdk2_python"


def ensure_sdk_path() -> None:
    sdk = str(SDK_DIR)
    if sdk not in sys.path:
        sys.path.insert(0, sdk)


def default_iface() -> str:
    return (os.environ.get("H2_DDS_IFACE") or "eth10").strip() or "eth10"


def smoothstep(t: float) -> float:
    t = float(np.clip(t, 0.0, 1.0))
    return t * t * (3.0 - 2.0 * t)


# G1/H2-style arm indices (unitree_hg)
LEFT_ARM_JOINTS = (15, 16, 17, 18, 19, 20, 21)
RIGHT_ARM_JOINTS = (22, 23, 24, 25, 26, 27, 28)
# H2: joint 29-30 = testa; peso arm_sdk su slot 31 (xr_teleoperate H2_JointIndex)
ARM_SDK_ENABLE_IDX = 31
LOCO_API_GET_ARM_SDK = 7007
LOCO_API_SET_ARM_SDK = 7109

H2_NUM_MOTOR = 31
# unitree_sdk2 example/h2/low_level/h2_ankle_swing_example.cpp
H2_KP = (
    150, 150, 150, 250, 60, 90,
    150, 150, 150, 250, 60, 90,
    200, 200, 200,
    90, 60, 20, 60, 4, 4, 4,
    90, 60, 20, 60, 4, 4, 4,
    30, 30,
)
H2_KD = (
    2.0, 2.0, 2.0, 2.0, 0.3, 0.1,
    2.0, 2.0, 2.0, 2.0, 0.3, 0.1,
    2.5, 5.0, 5.0,
    2.0, 1.0, 0.4, 1.0, 0.2, 0.2, 0.2,
    2.0, 1.0, 0.4, 1.0, 0.2, 0.2, 0.2,
    1.0, 1.0,
)
H2_ALL_JOINTS = tuple(range(H2_NUM_MOTOR))
# g1_arm7_sdk_dds_example.cpp comanda vita + entrambe le braccia (17 joint)
H2_ARM_SDK_JOINTS = (
    12, 13, 14,
    15, 16, 17, 18, 19, 20, 21,
    22, 23, 24, 25, 26, 27, 28,
)
# xr H2_ArmController gains
XR_KP_HIGH = 300.0
XR_KD_HIGH = 5.0
XR_KP_LOW = 140.0
XR_KD_LOW = 3.0
XR_KP_WRIST = 50.0
XR_KD_WRIST = 2.0
XR_WEAK_MOTORS = frozenset({4, 10, 15, 16, 17, 18, 22, 23, 24, 25})
XR_WRIST_MOTORS = frozenset({19, 20, 21, 26, 27, 28})
FSM_STAND_UP = 4
FSM_READY = 601
# Dal robot reale (GetAvailableFsmIds API 7008) — nomi firmware H2
FSM_NAMES: dict[int, str] = {
    0: "Invalid",
    1: "Passive",
    2: "Protection",
    3: "Sit",
    4: "FixStand",
    5: "HybridPassive",
    601: "HybridWalk",
    701: "WalkNew",
    703: "PhaseWalk",
}
# VIETATO muovere braccio in Damp/Passive — manda giù il robot
FSM_ARM_FORBIDDEN = frozenset({0, 1})
# Motion mode: locomozione attiva + protection frame — SENZA passare da Damp/FixStand
FSM_ARM_OK = frozenset({2, 4, 5, 601, 701, 703})
G1_OFFICIAL_KP = 60.0
G1_OFFICIAL_KD = 1.5

# Gentle pose: braccio sinistro demo principale (alto + rotazione polso)
LEFT_ARM_TARGET_Q = {
    15: -0.78,   # shoulder pitch — ancora più in alto
    16: 0.30,    # roll — braccio più verticale
    17: 0.10,    # yaw
    18: 0.45,    # gomito disteso verso l'alto
    19: 0.35,    # wrist roll
    20: -0.22,   # wrist pitch
    21: 0.15,    # wrist yaw
}

# Braccio destro avanti al petto (non sopra la testa)
RIGHT_ARM_FRONT_Q = {
    22: -0.85,  # shoulder pitch avanti
    23: -0.12,  # roll basso — resta sul piano frontale
    24: 0.0,
    25: 0.72,   # gomito piegato
    26: 0.0,
    27: 0.0,
    28: 0.0,
}

# Braccio destro avanti ad altezza bacino (mano ~fianchi)
RIGHT_ARM_WAIST_Q = {
    22: -0.48,
    23: -0.10,
    24: 0.0,
    25: 0.42,
    26: 0.0,
    27: 0.0,
    28: 0.0,
}


@dataclass
class GentleArmConfig:
    control_dt: float = 0.004  # 250 Hz (xr H2_ArmController)
    rise_s: float = 8.0
    hold_s: float = 2.0
    lower_s: float = 8.0
    release_s: float = 2.0
    kp: float = 90.0
    kd: float = 2.0


def init_dds(iface: str | None = None) -> str:
    ensure_sdk_path()
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize

    name = (iface or default_iface()).strip()
    ChannelFactoryInitialize(0, name)
    return name


def wait_lowstate(timeout_s: float = 15.0):
    ensure_sdk_path()
    from unitree_sdk2py.core.channel import ChannelSubscriber
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

    state = {"msg": None}

    def _handler(msg):
        state["msg"] = msg

    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(_handler, 10)
    t0 = time.time()
    while state["msg"] is None and time.time() - t0 < timeout_s:
        time.sleep(0.05)
    if state["msg"] is None:
        raise TimeoutError("rt/lowstate non ricevuto")
    n = len(state["msg"].motor_state)
    print(f"[h2] lowstate OK, motor_state count={n}")
    return state["msg"]


def _loco_client():
    ensure_sdk_path()
    from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

    loco = LocoClient()
    loco.SetTimeout(5.0)
    loco.Init()
    loco._RegistApi(LOCO_API_GET_ARM_SDK, 0)
    loco._RegistApi(LOCO_API_SET_ARM_SDK, 0)
    return loco


def get_h2_arm_sdk_status() -> tuple[int, bool | None]:
    import json

    loco = _loco_client()
    code, data = loco._Call(LOCO_API_GET_ARM_SDK, "{}")
    if code != 0 or not data:
        return code, None
    try:
        return code, bool(json.loads(data).get("data"))
    except Exception:
        return code, None


def set_h2_arm_sdk_enabled(enabled: bool) -> int:
    import json

    loco = _loco_client()
    code, _ = loco._Call(LOCO_API_SET_ARM_SDK, json.dumps({"data": bool(enabled)}))
    print(f"[h2] SetArmSdkStatus({enabled}) code={code}")
    return code


def get_h2_fsm_id() -> tuple[int, int | None]:
    loco = _loco_client()
    code, data = loco._Call(7001, "{}")
    if code != 0 or not data:
        return code, None
    import json

    try:
        return code, int(json.loads(data).get("data"))
    except Exception:
        return code, None


def fsm_label(fsm: int | None) -> str:
    if fsm is None:
        return "?"
    name = FSM_NAMES.get(fsm)
    return f"{fsm} ({name})" if name else str(fsm)


def check_h2_arm_motion_ready(force: bool = False) -> tuple[bool, str]:
    """Verifica prerequisiti Unitree per rt/arm_sdk in motion mode (no Damp)."""
    if force:
        return True, "skip (--force)"
    code, fsm = get_h2_fsm_id()
    if code != 0 or fsm is None:
        return False, f"FSM non leggibile (code={code})"
    if fsm in FSM_ARM_FORBIDDEN:
        return False, (
            f"FSM={fsm_label(fsm)} — NON muovere il braccio (Damp/Passive manda giù il robot). "
            "Ripristina HybridWalk/Protection dal telecomando, poi riprova."
        )
    if fsm not in FSM_ARM_OK:
        print(f"[h2] WARN FSM={fsm_label(fsm)} non in lista nota; provo comunque arm_sdk (motion mode)")
    code, enabled = get_h2_arm_sdk_status()
    if code != 0:
        return False, f"GetArmSdkStatus fallito (code={code})"
    return True, f"FSM={fsm_label(fsm)} arm_sdk_api={enabled} (motion mode, no Damp)"


def _xr_joint_gains(j: int) -> tuple[float, float]:
    if j in XR_WRIST_MOTORS:
        return XR_KP_WRIST, XR_KD_WRIST
    if j in XR_WEAK_MOTORS or j in RIGHT_ARM_JOINTS or j in LEFT_ARM_JOINTS:
        return XR_KP_LOW, XR_KD_LOW
    return XR_KP_HIGH, XR_KD_HIGH


def _lock_h2_xr_lowcmd(low_cmd, low_state) -> dict[int, float]:
    """Blocca tutti i joint corpo H2 (pattern xr H2_ArmController)."""
    locked_q: dict[int, float] = {}
    low_cmd.mode_pr = 0
    low_cmd.mode_machine = int(low_state.mode_machine)
    for i in H2_ALL_JOINTS:
        q = float(low_state.motor_state[i].q)
        locked_q[i] = q
        kp, kd = _xr_joint_gains(i)
        low_cmd.motor_cmd[i].mode = 1
        low_cmd.motor_cmd[i].q = q
        low_cmd.motor_cmd[i].dq = 0.0
        low_cmd.motor_cmd[i].tau = 0.0
        low_cmd.motor_cmd[i].kp = kp
        low_cmd.motor_cmd[i].kd = kd
    return locked_q


def _fill_g1_arm_joint(cmd, j: int, q: float) -> None:
    """g1_arm7_sdk_dds_example.cpp — solo q/dq/tau/kp/kd, no mode."""
    cmd.motor_cmd[j].q = q
    cmd.motor_cmd[j].dq = 0.0
    cmd.motor_cmd[j].tau = 0.0
    cmd.motor_cmd[j].kp = G1_OFFICIAL_KP
    cmd.motor_cmd[j].kd = G1_OFFICIAL_KD


def _fill_arm_sdk_joint(cmd, j: int, q: float) -> None:
    _fill_g1_arm_joint(cmd, j, q)


def release_motion_modes(max_tries: int = 8) -> None:
    """NON usare in demo H2: rilascia la modalità locomozione (es. ai) e può far cadere il robot.

    Presente solo per debug esplicito con --release-modes. L'esempio ufficiale G1 arm_sdk
    non chiama MotionSwitcher.
    """
    raise RuntimeError(
        "release_motion_modes() disabilitato per sicurezza. "
        "Usa solo rt/arm_sdk come g1_arm7_sdk_dds_example.py."
    )


def run_gentle_arm(
    joints: tuple[int, ...],
    target_q: dict[int, float],
    cfg: GentleArmConfig | None = None,
    on_phase: Callable[[str], None] | None = None,
    label: str = "arm",
    force_fsm: bool = False,
) -> None:
    """Ramp su rt/arm_sdk in motion mode — xr H2_ArmController (tutti i joint bloccati)."""
    ok, msg = check_h2_arm_motion_ready(force=force_fsm)
    print(f"[h2] arm precheck: {msg}")
    if not ok:
        raise RuntimeError(msg)

    ensure_sdk_path()
    from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
    from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
    from unitree_sdk2py.utils.crc import CRC
    from unitree_sdk2py.utils.thread import RecurrentThread

    cfg = cfg or GentleArmConfig()
    low_state = {"msg": None}

    def _handler(msg: LowState_):
        low_state["msg"] = msg

    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(_handler, 10)
    t0 = time.time()
    while low_state["msg"] is None and time.time() - t0 < 15.0:
        time.sleep(0.05)
    if low_state["msg"] is None:
        raise TimeoutError("lowstate mancante per arm_sdk")

    st0 = low_state["msg"]
    start_q = {j: float(st0.motor_state[j].q) for j in joints}
    low_cmd = unitree_hg_msg_dds__LowCmd_()
    locked_q = _lock_h2_xr_lowcmd(low_cmd, st0)
    crc = CRC()
    t_start = {"v": 0.0}
    done = {"v": False}
    n_pub = {"v": 0}

    def _phase(name: str) -> None:
        if on_phase:
            on_phase(name)

    pub = ChannelPublisher("rt/arm_sdk", LowCmd_)
    pub.Init()

    # xr motion_mode: peso su DDS slot 31 — non disabilitare arm_sdk (evita transizioni pericolose)
    code, enabled = get_h2_arm_sdk_status()
    if code == 0 and not enabled:
        set_h2_arm_sdk_enabled(True)
        time.sleep(0.15)

    total_s = cfg.rise_s + cfg.hold_s + cfg.lower_s + cfg.release_s
    print(
        f"[h2] arm_sdk xr-motion weight_idx={ARM_SDK_ENABLE_IDX} "
        f"mode_machine={int(st0.mode_machine)} joints={joints} dt={cfg.control_dt}"
    )

    def _write():
        st = low_state["msg"]
        if st is not None:
            low_cmd.mode_machine = int(st.mode_machine)

        if t_start["v"] == 0.0:
            t_start["v"] = time.time()
        elapsed = time.time() - t_start["v"]

        if elapsed < cfg.rise_s:
            _phase("rise")
            r = smoothstep(elapsed / cfg.rise_s)
            weight = 1.0
        elif elapsed < cfg.rise_s + cfg.hold_s:
            _phase("hold")
            r = 1.0
            weight = 1.0
        elif elapsed < cfg.rise_s + cfg.hold_s + cfg.lower_s:
            _phase("lower")
            t = (elapsed - cfg.rise_s - cfg.hold_s) / cfg.lower_s
            r = 1.0 - smoothstep(t)
            weight = 1.0
        elif elapsed < total_s:
            _phase("release")
            rel = (elapsed - cfg.rise_s - cfg.hold_s - cfg.lower_s) / cfg.release_s
            weight = 1.0 - smoothstep(rel)
            r = 0.0
        else:
            done["v"] = True
            return

        low_cmd.motor_cmd[ARM_SDK_ENABLE_IDX].q = weight
        for i in H2_ALL_JOINTS:
            if i == ARM_SDK_ENABLE_IDX:
                continue
            if i in joints:
                q0 = start_q[i]
                tgt = target_q[i]
                low_cmd.motor_cmd[i].q = (1.0 - r) * q0 + r * tgt
            else:
                low_cmd.motor_cmd[i].q = locked_q[i]
            kp, kd = _xr_joint_gains(i)
            low_cmd.motor_cmd[i].mode = 1
            low_cmd.motor_cmd[i].dq = 0.0
            low_cmd.motor_cmd[i].tau = 0.0
            low_cmd.motor_cmd[i].kp = kp
            low_cmd.motor_cmd[i].kd = kd

        low_cmd.crc = crc.Crc(low_cmd)
        pub.Write(low_cmd)
        n_pub["v"] += 1

    try:
        thread = RecurrentThread(interval=cfg.control_dt, target=_write, name="h2_arm")
        thread.Start()
        last_log = time.time()
        while not done["v"]:
            time.sleep(0.05)
            if time.time() - last_log >= 1.0 and low_state["msg"] is not None:
                qs = " ".join(
                    f"j{j}={float(low_state['msg'].motor_state[j].q):.4f}" for j in joints
                )
                print(f"[h2] live {qs} pubs={n_pub['v']}")
                last_log = time.time()
    finally:
        low_cmd.motor_cmd[ARM_SDK_ENABLE_IDX].q = 0.0
        low_cmd.crc = crc.Crc(low_cmd)
        pub.Write(low_cmd)
    st1 = low_state["msg"]
    if st1 is not None:
        for j in joints:
            print(f"[h2] joint {j}: start={start_q[j]:.4f} end={float(st1.motor_state[j].q):.4f}")
    print(f"[h2] gentle {label} done (pubs={n_pub['v']})")


def run_gentle_left_arm(
    cfg: GentleArmConfig | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> None:
    run_gentle_arm(LEFT_ARM_JOINTS, LEFT_ARM_TARGET_Q, cfg, on_phase, label="left arm")


def run_gentle_right_arm_front(
    cfg: GentleArmConfig | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> None:
    run_gentle_arm(RIGHT_ARM_JOINTS, RIGHT_ARM_FRONT_Q, cfg, on_phase, label="right arm front")


def run_gentle_right_arm_waist(
    cfg: GentleArmConfig | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> None:
    run_gentle_arm(RIGHT_ARM_JOINTS, RIGHT_ARM_WAIST_Q, cfg, on_phase, label="right arm waist")


# Micromovimenti dalla posa attuale (radianti) — braccio destro
RIGHT_ARM_MICRO_DELTA = {
    22: -0.10,  # spalla leggermente avanti
    23: -0.05,
    25: 0.08,   # gomito un po' piegato
}


def run_micro_right_arm(
    deltas: dict[int, float] | None = None,
    cfg: GentleArmConfig | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> None:
    """Piccolo movimento relativo, molto lento — per primo test in lab."""
    st = wait_lowstate()
    d = deltas or RIGHT_ARM_MICRO_DELTA
    target_q: dict[int, float] = {}
    for j in RIGHT_ARM_JOINTS:
        q0 = float(st.motor_state[j].q)
        target_q[j] = q0 + d.get(j, 0.0)
    print("[h2] micro right arm deltas (rad):")
    for j in RIGHT_ARM_JOINTS:
        if j in d:
            print(f"  joint {j}: {st.motor_state[j].q:.3f} -> {target_q[j]:.3f} (d={d[j]:+.3f})")
    cfg = cfg or GentleArmConfig(rise_s=12.0, hold_s=2.0, lower_s=12.0, release_s=2.0)
    run_gentle_arm(RIGHT_ARM_JOINTS, target_q, cfg, on_phase, label="right arm micro")


def emergency_stop_arm_sdk(
    joints: tuple[int, ...] = RIGHT_ARM_JOINTS,
    hold_s: float = 0.4,
    release_s: float = 0.35,
) -> None:
    """Rilascia subito rt/arm_sdk (slot 31 su H2) — xr lock su tutti i joint."""
    ensure_sdk_path()
    from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
    from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
    from unitree_sdk2py.utils.crc import CRC

    low_state = {"msg": None}

    def _handler(msg: LowState_):
        low_state["msg"] = msg

    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(_handler, 10)
    t0 = time.time()
    while low_state["msg"] is None and time.time() - t0 < 3.0:
        time.sleep(0.02)

    low_cmd = unitree_hg_msg_dds__LowCmd_()
    crc = CRC()
    pub = ChannelPublisher("rt/arm_sdk", LowCmd_)
    pub.Init()
    locked_q: dict[int, float] = {}
    if low_state["msg"] is not None:
        locked_q = _lock_h2_xr_lowcmd(low_cmd, low_state["msg"])

    t_start = time.time()
    total = hold_s + release_s

    while True:
        elapsed = time.time() - t_start
        if elapsed >= total:
            break
        weight = 1.0 if elapsed < hold_s else 1.0 - min(1.0, (elapsed - hold_s) / release_s)
        if low_state["msg"] is not None:
            low_cmd.mode_machine = int(low_state["msg"].mode_machine)

        low_cmd.motor_cmd[ARM_SDK_ENABLE_IDX].q = weight
        for i in H2_ALL_JOINTS:
            if i == ARM_SDK_ENABLE_IDX:
                continue
            low_cmd.motor_cmd[i].q = locked_q.get(i, 0.0)
            kp, kd = _xr_joint_gains(i)
            low_cmd.motor_cmd[i].mode = 1
            low_cmd.motor_cmd[i].dq = 0.0
            low_cmd.motor_cmd[i].tau = 0.0
            low_cmd.motor_cmd[i].kp = kp
            low_cmd.motor_cmd[i].kd = kd
        low_cmd.crc = crc.Crc(low_cmd)
        pub.Write(low_cmd)
        time.sleep(0.004)

    low_cmd.motor_cmd[ARM_SDK_ENABLE_IDX].q = 0.0
    low_cmd.crc = crc.Crc(low_cmd)
    pub.Write(low_cmd)
    print("[h2] emergency arm_sdk release done")

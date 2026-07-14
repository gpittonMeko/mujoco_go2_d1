"""BrainCo Revo 2 hand helpers for H2 (rt/brainco/right/cmd|state)."""
from __future__ import annotations

import time

MOTOR_NUM = 6
BRAINCO_RIGHT_CMD = "rt/brainco/right/cmd"
BRAINCO_RIGHT_STATE = "rt/brainco/right/state"
BRAINCO_LEFT_CMD = "rt/brainco/left/cmd"
BRAINCO_LEFT_STATE = "rt/brainco/left/state"

# q in [0, 1]: 0=open, 1=closed — see unitree brainco_hand_service / xr_teleoperate
# [thumb, thumb-aux, index, middle, ring, pinky]
FINGER_NAMES = ("thumb", "thumb-aux", "index", "middle", "ring", "pinky")


def finger_targets_right(close_fraction: float) -> tuple[float, ...]:
    """0=open, 1=closed — gentle partial close, thumb slightly less."""
    f = max(0.0, min(0.98, close_fraction))
    return (f * 0.65, f * 0.55, f, f, f, f)


def finger_targets_left(close_fraction: float) -> tuple[float, ...]:
    return finger_targets_right(close_fraction)


def wait_hand_state(topic: str, timeout_s: float = 5.0):
    from unitree_sdk2py.core.channel import ChannelSubscriber
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorStates_

    state = {"msg": None}

    def _handler(msg: MotorStates_):
        if msg is not None and len(msg.states) >= MOTOR_NUM:
            state["msg"] = msg

    sub = ChannelSubscriber(topic, MotorStates_)
    sub.Init(_handler, 10)
    t0 = time.time()
    while state["msg"] is None and time.time() - t0 < timeout_s:
        time.sleep(0.05)
    return state["msg"]


def wait_right_hand_state(topic: str = BRAINCO_RIGHT_STATE, timeout_s: float = 5.0):
    return wait_hand_state(topic, timeout_s)


def wait_left_hand_state(topic: str = BRAINCO_LEFT_STATE, timeout_s: float = 5.0):
    return wait_hand_state(topic, timeout_s)


def probe_right_hand(timeout_s: float = 5.0) -> tuple[str | None, object | None]:
    msg = wait_right_hand_state(BRAINCO_RIGHT_STATE, timeout_s=timeout_s)
    if msg is not None:
        qs = [f"{msg.states[i].q:.3f}" for i in range(MOTOR_NUM)]
        print(f"[hand] OK BrainCo Revo2 topic={BRAINCO_RIGHT_STATE}")
        print(f"[hand] q={list(zip(FINGER_NAMES, qs))}")
        return BRAINCO_RIGHT_STATE, msg
    print(f"[hand] no data on {BRAINCO_RIGHT_STATE}")
    print(
        "[hand] hint: nessun publisher DDS rt/brainco/* su eth10 — "
        "il bridge seriale→DDS gira sul PC2 (.162), non sulla Thor dev."
    )
    return None, None


def probe_left_hand(timeout_s: float = 5.0) -> tuple[str | None, object | None]:
    msg = wait_left_hand_state(BRAINCO_LEFT_STATE, timeout_s=timeout_s)
    if msg is not None:
        qs = [f"{msg.states[i].q:.3f}" for i in range(MOTOR_NUM)]
        print(f"[hand] OK BrainCo Revo2 topic={BRAINCO_LEFT_STATE}")
        print(f"[hand] q={list(zip(FINGER_NAMES, qs))}")
        return BRAINCO_LEFT_STATE, msg
    print(f"[hand] no data on {BRAINCO_LEFT_STATE}")
    return None, None


def run_gentle_grip(
    side: str = "right",
    close_fraction: float = 0.45,
    rise_s: float = 2.0,
    hold_s: float = 1.5,
    lower_s: float = 2.0,
) -> None:
    side = side.lower()
    if side == "left":
        state_topic = BRAINCO_LEFT_STATE
        cmd_topic = BRAINCO_LEFT_CMD
        targets = finger_targets_left
        label = "left"
    else:
        state_topic = BRAINCO_RIGHT_STATE
        cmd_topic = BRAINCO_RIGHT_CMD
        targets = finger_targets_right
        label = "right"

    from unitree_sdk2py.core.channel import ChannelPublisher
    from unitree_sdk2py.idl.default import unitree_go_msg_dds__MotorCmd_
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorCmds_

    state_msg = wait_hand_state(state_topic, timeout_s=3.0)
    if state_msg is not None:
        open_q = tuple(float(state_msg.states[i].q) for i in range(MOTOR_NUM))
    else:
        open_q = (0.0,) * MOTOR_NUM

    grip_q = targets(close_fraction)
    msg = MotorCmds_()
    msg.cmds = [unitree_go_msg_dds__MotorCmd_() for _ in range(MOTOR_NUM)]
    for i in range(MOTOR_NUM):
        msg.cmds[i].dq = 1.0

    pub = ChannelPublisher(cmd_topic, MotorCmds_)
    pub.Init()

    def _send(qs: tuple[float, ...]) -> None:
        for i in range(MOTOR_NUM):
            msg.cmds[i].q = float(qs[i])
        pub.Write(msg)

    def _ramp(t_s: float, q0: tuple[float, ...], q1: tuple[float, ...], phase: str) -> None:
        t0 = time.time()
        while True:
            u = (time.time() - t0) / t_s
            if u >= 1.0:
                break
            from h2_common import smoothstep

            r = smoothstep(u)
            qs = tuple((1 - r) * q0[i] + r * q1[i] for i in range(MOTOR_NUM))
            _send(qs)
            time.sleep(0.02)
        _send(q1)
        print(f"[hand] {label} {phase}")

    print(f"[hand] BrainCo Revo2 {label} gentle close fraction={close_fraction}")
    _ramp(rise_s, open_q, grip_q, "close")
    time.sleep(hold_s)
    _ramp(lower_s, grip_q, open_q, "open")
    print(f"[hand] {label} gentle grip done")


def run_gentle_right_grip(
    close_fraction: float = 0.45,
    rise_s: float = 2.0,
    hold_s: float = 1.5,
    lower_s: float = 2.0,
    cmd_topic: str = BRAINCO_RIGHT_CMD,
) -> None:
    del cmd_topic  # legacy arg
    run_gentle_grip("right", close_fraction, rise_s, hold_s, lower_s)


def run_gentle_left_grip(
    close_fraction: float = 0.45,
    rise_s: float = 2.0,
    hold_s: float = 1.5,
    lower_s: float = 2.0,
) -> None:
    run_gentle_grip("left", close_fraction, rise_s, hold_s, lower_s)

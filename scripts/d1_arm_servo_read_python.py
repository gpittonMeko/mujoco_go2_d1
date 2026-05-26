#!/usr/bin/env python3
"""
Legge ``current_servo_angle`` (PubServoInfo_, 7 float) via unitree_sdk2py — stesso DDS di Sport/Crouch.

Uso (stesso contratto di ``bin/d1_arm_feedback_helper``):
  python3 scripts/d1_arm_servo_read_python.py <domain> <listen_s>

``listen_s`` = attesa massima; si esce dopo il primo ``servo_angles`` + settle (env ``D1_FEEDBACK_PYTHON_SETTLE_S``, default 0.08 s).
Stampa righe ``servo_angles`` e a fine ``servo_count=`` / ``feedback_count=`` (feedback_count=0).
Env: GO2_DDS_INTERFACE (es. eth0), PYTHONPATH con ``unitree_sdk2_python`` se non dalla root repo.
"""
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# --- IDL minimale (7 float32) allineato a msg/PubServoInfo_.hpp Cyclone 0.10.x ---

try:
    import cyclonedds.idl as idl
    import cyclonedds.idl.annotations as annotate
    import cyclonedds.idl.types as types
except ImportError as exc:
    print("servo_count=0", flush=True)
    print("feedback_count=0", flush=True)
    print(f"FATAL_IMPORT cyclonedds: {exc}", file=sys.stderr, flush=True)
    raise SystemExit(2) from exc


@dataclass
@annotate.final
@annotate.autoid("sequential")
class PubServoInfo_(idl.IdlStruct, typename="unitree_arm.msg.dds_.PubServoInfo_"):
    servo0_data: types.float32
    servo1_data: types.float32
    servo2_data: types.float32
    servo3_data: types.float32
    servo4_data: types.float32
    servo5_data: types.float32
    servo6_data: types.float32


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main() -> int:
    domain = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    listen_s = max(1.0, float(sys.argv[2]) if len(sys.argv) > 2 else 3.0)
    settle_s = max(0.0, min(0.8, float(os.environ.get("D1_FEEDBACK_PYTHON_SETTLE_S", "0.08") or "0.08")))
    root = _project_root()
    usdk = root / "unitree_sdk2_python"
    if usdk.is_dir():
        p = str(usdk)
        if p not in sys.path:
            sys.path.insert(0, p)

    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber

    iface = (os.environ.get("GO2_DDS_INTERFACE") or "").strip() or None
    if iface:
        ChannelFactoryInitialize(domain, iface)
    else:
        ChannelFactoryInitialize(domain)

    topic_names = ("current_servo_angle", "rt/current_servo_angle")
    servo_count = 0
    feedback_count = 0
    deadline = time.monotonic() + listen_s

    sub = None
    used_topic = None
    last_exc: BaseException | None = None
    for tn in topic_names:
        try:
            cand = ChannelSubscriber(tn, PubServoInfo_)
            cand.Init()
            sub = cand
            used_topic = tn
            break
        except BaseException as exc:
            last_exc = exc
            print(f"WARN topic {tn!r} init failed: {exc!r}", file=sys.stderr, flush=True)
            continue
    if sub is None:
        print("servo_count=0", flush=True)
        print("feedback_count=0", flush=True)
        print("FATAL no ChannelSubscriber init for topics", topic_names, file=sys.stderr, flush=True)
        if last_exc is not None:
            print(f"last_error={last_exc!r}", file=sys.stderr, flush=True)
        return 3

    latest_line = None
    first_sample_at: float | None = None
    while time.monotonic() < deadline:
        try:
            msg = sub.Read(timeout=0.35)
        except Exception:
            msg = None
        if msg is None:
            if first_sample_at is not None and time.monotonic() - first_sample_at >= settle_s:
                break
            continue
        servo_count += 1
        try:
            vals = []
            for i in range(7):
                v = None
                for name in (f"servo{i}_data", f"servo{i}_data_"):
                    if hasattr(msg, name):
                        v = float(getattr(msg, name))
                        break
                if v is None:
                    raise ValueError(f"missing servo{i}_data")
                vals.append(v)
        except Exception:
            continue
        latest_line = "servo_angles " + " ".join(str(v) for v in vals)
        print(latest_line, flush=True)
        if first_sample_at is None:
            first_sample_at = time.monotonic()
        elif settle_s > 0 and time.monotonic() - first_sample_at >= settle_s:
            break

    try:
        sub.Close()
    except Exception:
        pass

    print(f"servo_count={servo_count} feedback_count={feedback_count} topic={used_topic!r}", flush=True)
    return 0 if servo_count > 0 or feedback_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

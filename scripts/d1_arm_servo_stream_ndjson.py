#!/usr/bin/env python3
"""
Stream angoli servo D1 direttamente da DDS (senza HTTP / senza spawn ripetuti di subprocess).

Il messaggio ``PubServoInfo_`` su ``current_servo_angle`` è ciò che leggono anche gli helper HTTP.
**Nota Unitree:** il feedback giunti è tipicamente dell’ordine di ~10 Hz dal servizio braccio:
questo script stampa **ogni** campione ricevuto (non uno snapshot ogni 300 ms come una GET Flask).

Per correlare una **caduta fisica** instantanea:
  T1 (NX): ``python3 scripts/d1_arm_servo_stream_ndjson.py 0 90 > /tmp/arm_stream.ndjson``
  T2 (PC o NX): comando dashboard / ``move_one``

Se i numeri DDS restano “belli” mentre il braccio crolla, è probabile **disaccoppiamento meccanico/
encoder sul motore**, **alimentazione**, oppure **topic che non riflette la cinematica endeffector**.

Uso::
  python3 scripts/d1_arm_servo_stream_ndjson.py [dds_domain] [duration_s]

Env: ``GO2_DDS_INTERFACE`` (es. eth0), ``PYTHONPATH`` con ``unitree_sdk2_python`` se serve.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import cyclonedds.idl as idl
    import cyclonedds.idl.annotations as annotate
    import cyclonedds.idl.types as types
except ImportError as exc:
    print(json.dumps({"ok": False, "fatal": "cyclonedds_import", "err": repr(exc)}), flush=True)
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


def _vals7(msg: object) -> list[float] | None:
    vals = []
    try:
        for i in range(7):
            v = None
            for name in (f"servo{i}_data", f"servo{i}_data_"):
                if hasattr(msg, name):
                    v = float(getattr(msg, name))
                    break
            if v is None:
                return None
            vals.append(v)
    except Exception:
        return None
    return vals


def main() -> int:
    domain = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("GO2_DDS_DOMAIN", "0"))
    duration_s = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    duration_s = max(1.0, min(86400.0, duration_s))

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
            continue
    if sub is None:
        print(
            json.dumps({"ok": False, "fatal": "no_subscriber", "topics": list(topic_names), "last": repr(last_exc)}),
            flush=True,
        )
        return 3

    t0 = time.monotonic()
    deadline = t0 + duration_s
    last_recv = t0
    n = 0
    print(
        json.dumps(
            {
                "ok": True,
                "event": "stream_start",
                "domain": domain,
                "topic": used_topic,
                "duration_s": duration_s,
                "note_hz": "~10Hz tipico dal firmware braccio (ogni riga = un messaggio DDS)",
            },
            ensure_ascii=False,
        ),
        flush=True,
        file=sys.stderr,
    )

    try:
        while time.monotonic() < deadline:
            try:
                msg = sub.Read(timeout=0.25)
            except Exception:
                msg = None
            now = time.monotonic()
            if msg is None:
                continue
            vals = _vals7(msg)
            if vals is None:
                continue
            n += 1
            dt_ms = (now - last_recv) * 1000.0 if n > 1 else None
            last_recv = now
            line = {
                "mono_s": round(now - t0, 4),
                "wall_unix": round(time.time(), 3),
                "dt_since_prev_ms": None if dt_ms is None else round(dt_ms, 2),
                "servo_deg": [round(v, 4) for v in vals],
            }
            print(json.dumps(line, separators=(",", ":"), ensure_ascii=False), flush=True)
    except KeyboardInterrupt:
        print(json.dumps({"ok": True, "event": "interrupt", "samples": n}, ensure_ascii=False), file=sys.stderr, flush=True)
    finally:
        try:
            sub.Close()
        except Exception:
            pass

    print(json.dumps({"ok": True, "event": "stream_end", "samples": n}, ensure_ascii=False), file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

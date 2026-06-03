#!/usr/bin/env python3
"""
Prototipo accompagnamento braccio D1 via rt/arm_Command (solo software, non firmware UR).

Modalità predefinita **echo** («molle» lato PC): ogni tick comanda **gli angoli letti adesso** (funcode 2).
Massimo avvicinamento software al «segui la mano» senza API torque-off Unitree — **non** spegne i motori.

Modalità **passthrough**: filtro esponenziale + slew (stabile se l’eco jittera).

Modalità **mirror** (legacy): il comando inseguie **lentamente**
la posa letta da feedback:
  q_cmd ← q_cmd + η · (q_misurato − q_cmd)
così il servo non deve recuperare grandi errori di posizione quando spingi a mano — η basso = segue
“piano piano” (come nei tuoi campioni con delta piccoli tra una lettura e l’altra).

Modalità **assist**: rinforzo lungo la velocità filtrata (Delta q) — vedi --mode assist.

ATTENZIONE — solo laboratorio, area libera, operatore pronto al kill.

Esempio (default = echo):
  python3 scripts/d1_drag_follow_experimental.py

Stop: Ctrl+C — oppure dashboard «Stop drag» (POST /api/arm/drag_follow enable:false).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _latest_servo_from_line(line: str) -> list[float] | None:
    line = line.strip()
    if not line.startswith("servo_angles "):
        return None
    parts = line.split()[1:]
    if len(parts) < 7:
        return None
    try:
        return [float(parts[i]) for i in range(7)]
    except ValueError:
        return None


def _feedback_reader(proc: subprocess.Popen, lock: threading.Lock, slot: list[list[float] | None]) -> None:
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            q = _latest_servo_from_line(raw)
            if q is not None:
                with lock:
                    slot[0] = q
    except Exception:
        pass


def _drag_follow_diag_jsonl_append(
    fp,
    *,
    mode: str,
    q_now: list[float],
    cmd_q: list[float],
    tick_count: int,
    period_ms: float,
    prev_fb: list[float] | None,
) -> list[float]:
    """Una riga JSON per ~log_interval: confronto feedback vs comando (debug giunti bloccati)."""
    dq = [round(q_now[i] - prev_fb[i], 5) for i in range(7)] if prev_fb else [0.0] * 7
    err = [round(cmd_q[i] - q_now[i], 5) for i in range(7)]
    row = {
        "event": "tick_summary",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": mode,
        "tick": tick_count,
        "period_ms": round(period_ms, 2),
        "fb_deg": [round(q_now[i], 4) for i in range(7)],
        "cmd_deg": [round(cmd_q[i], 4) for i in range(7)],
        "dq_fb_deg": dq,
        "err_cmd_minus_fb_deg": err,
    }
    fp.write(json.dumps(row, separators=(",", ":")) + "\n")
    fp.flush()
    return [float(q_now[i]) for i in range(7)]


def main() -> int:
    parser = argparse.ArgumentParser(description="D1 drag-follow: echo / passthrough / mirror / assist")
    parser.add_argument("--domain", type=int, default=int(os.environ.get("GO2_DDS_DOMAIN", "0")))
    parser.add_argument(
        "--mode",
        choices=("mirror", "assist", "passthrough", "echo"),
        default="echo",
        help="echo=comanda q misurato ogni tick (max morbido SW); passthrough=filtro+slew; mirror=η; assist=Delta q",
    )
    parser.add_argument(
        "--track-eta",
        type=float,
        default=0.56,
        help="mirror: passo verso q_misurato per tick (più alto = più fluido; clamp ~0.94)",
    )
    parser.add_argument(
        "--mirror-max-step-deg",
        type=float,
        default=1.2,
        help="mirror: limite |step| per giunto/tick (se troppo basso = clipping continuo = rigido)",
    )
    parser.add_argument(
        "--gripper-mirror-scale",
        type=float,
        default=1.0,
        help="mirror: moltiplicatore cap sul giunto 6 (1.0 = come gli altri)",
    )
    parser.add_argument(
        "--mirror-base-count",
        type=int,
        default=3,
        help="mirror: primi N giunti (0..N-1) spesso più rigidi — η e cap moltiplicati (default 3 = giunti 0,1,2)",
    )
    parser.add_argument(
        "--mirror-base-eta-scale",
        type=float,
        default=3.4,
        help="mirror: moltiplicatore η sui primi --mirror-base-count giunti (base/spalla)",
    )
    parser.add_argument(
        "--mirror-base-cap-scale",
        type=float,
        default=1.85,
        help="mirror: moltiplicatore cap step sui primi giunti base (più margine vs attrito/statico)",
    )
    parser.add_argument("--gain", type=float, default=0.18, help="assist: rinforzo su Delta q filtrato")
    parser.add_argument("--hz", type=float, default=14.0, help="frequenza anello mirror (Hz); più alto = più fluido")
    parser.add_argument("--seconds", type=float, default=180.0, help="durata massima (s)")
    parser.add_argument("--max-step-deg", type=float, default=0.55, help="assist: limite step lungo delta")
    parser.add_argument("--deadband-deg", type=float, default=0.04, help="assist: sotto questo |Delta| non comandare")
    parser.add_argument("--smooth", type=float, default=0.55, help="assist: smoothing su Delta q")
    parser.add_argument("--command-delay-ms", type=int, default=30, help="sleep nel publisher C++ tra messaggi (più basso = comandi più frequenti)")
    parser.add_argument(
        "--passthrough-alpha",
        type=float,
        default=0.88,
        help="passthrough: 1=solo misura filtrata implicita via slew; più basso = più smoothing",
    )
    parser.add_argument(
        "--passthrough-max-step-deg",
        type=float,
        default=6.0,
        help="passthrough: max |Δ°| comando per giunto/tick (slew limit)",
    )
    parser.add_argument(
        "--echo-base-lead",
        type=float,
        default=0.55,
        help="echo: anticipo += gain*(q_now-prev_fb) sui giunti pesanti (0=off); mitiga J1/J2 bloccati",
    )
    parser.add_argument(
        "--echo-lead-cap-deg",
        type=float,
        default=5.0,
        help="echo: max |°| correzione lead per tick",
    )
    parser.add_argument(
        "--echo-heavy-count",
        type=int,
        default=4,
        help="echo: primi N giunti con lead + più decimali (spalla/gomito)",
    )
    parser.add_argument("--echo-decimals-heavy", type=int, default=5, help="echo: decimali giunti pesanti")
    parser.add_argument("--echo-decimals-rest", type=int, default=3, help="echo: decimali altri giunti")
    parser.add_argument("--feedback-run-s", type=int, default=7200, help="durata processo feedback (secondi)")
    parser.add_argument(
        "--log-file",
        type=str,
        default="",
        help="path file log (es. data/drag_follow_loop.log); max_err, clipping, timing",
    )
    parser.add_argument("--log-interval-s", type=float, default=1.0, help="intervallo righe di log")
    parser.add_argument(
        "--diag-jsonl",
        type=str,
        default="",
        help="path JSONL diagnostico (fb/cmd/dq); vuoto = off",
    )
    args = parser.parse_args()

    fb_bin = PROJECT_ROOT / "bin" / "d1_arm_feedback_helper"
    cmd_bin = PROJECT_ROOT / "bin" / "d1_arm_command"
    if not fb_bin.is_file() or not cmd_bin.is_file():
        print("ERROR: missing bin/d1_arm_feedback_helper or bin/d1_arm_command", file=sys.stderr)
        return 2

    slot: list[list[float] | None] = [None]
    lock = threading.Lock()

    fb_proc = subprocess.Popen(
        [str(fb_bin), str(args.domain), str(args.feedback_run_s)],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    reader = threading.Thread(target=_feedback_reader, args=(fb_proc, lock, slot), daemon=True)
    reader.start()

    cur: list[float] | None = None
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        with lock:
            cur = slot[0]
        if cur is not None:
            break
        time.sleep(0.05)
    if cur is None:
        print("ERROR: no servo_angles from feedback within 15s", file=sys.stderr)
        fb_proc.terminate()
        try:
            fb_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            fb_proc.kill()
        return 3

    cmd_proc = subprocess.Popen(
        [str(cmd_bin), str(args.domain), str(args.command_delay_ms)],
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert cmd_proc.stdin is not None

    seq_counter = int(time.time()) % 100000
    init_msg = {"seq": seq_counter, "address": 1, "funcode": 5, "data": {"mode": 1}}
    cmd_proc.stdin.write(json.dumps(init_msg, separators=(",", ":")) + "\n")
    cmd_proc.stdin.flush()
    time.sleep(0.25)
    seq_counter += 1

    stop = threading.Event()

    def _sig(_s, _f):
        stop.set()

    signal.signal(signal.SIGINT, _sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sig)

    prev = [float(x) for x in cur]
    q_cmd = [float(x) for x in cur]
    end_t = time.monotonic() + float(args.seconds)
    period = 1.0 / max(2.0, float(args.hz))

    eta = max(0.03, min(0.94, float(args.track_eta)))
    mcap = max(0.05, float(args.mirror_max_step_deg))
    gms = max(0.1, min(1.5, float(args.gripper_mirror_scale)))
    n_base = max(0, min(7, int(args.mirror_base_count)))
    base_eta_mul = max(1.0, min(4.85, float(args.mirror_base_eta_scale)))
    base_cap_mul = max(1.0, min(3.2, float(args.mirror_base_cap_scale)))

    log_fp = None
    log_path = (PROJECT_ROOT / args.log_file).resolve() if args.log_file.strip() else None
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_fp = log_path.open("a", encoding="utf-8")
            log_fp.write(
                f"\n# drag_follow start {time.strftime('%Y-%m-%dT%H:%M:%S')} "
                f"mode={args.mode} eta={eta} hz={args.hz} mcap={mcap} cmd_delay_ms={args.command_delay_ms} "
                f"pt_a={float(args.passthrough_alpha):g} pt_cap={float(args.passthrough_max_step_deg):g} "
                f"echo_lead={float(args.echo_base_lead):g} echo_nh={int(args.echo_heavy_count)}\n"
            )
            log_fp.flush()
        except OSError as exc:
            print(f"WARN log-file {log_path}: {exc}", file=sys.stderr)

    diag_jsonl_fp = None
    diag_path = (PROJECT_ROOT / args.diag_jsonl).resolve() if str(args.diag_jsonl).strip() else None
    if diag_path is not None:
        try:
            diag_path.parent.mkdir(parents=True, exist_ok=True)
            diag_jsonl_fp = diag_path.open("a", encoding="utf-8")
            diag_jsonl_fp.write(
                json.dumps(
                    {
                        "event": "session_start",
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "mode": args.mode,
                        "hz": float(args.hz),
                        "domain": int(args.domain),
                        "command_delay_ms": int(args.command_delay_ms),
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            diag_jsonl_fp.flush()
        except OSError as exc:
            print(f"WARN diag-jsonl {diag_path}: {exc}", file=sys.stderr)
            diag_jsonl_fp = None

    pt_alpha = max(0.05, min(1.0, float(args.passthrough_alpha)))
    pt_cap = max(0.15, float(args.passthrough_max_step_deg))

    print(
        f"drag_follow start mode={args.mode} domain={args.domain} hz={args.hz} cmd_delay_ms={args.command_delay_ms} "
        f"eta={eta if args.mode == 'mirror' else 'n/a'} "
        f"gain={args.gain if args.mode == 'assist' else 'n/a'} "
        f"pt_α={pt_alpha if args.mode == 'passthrough' else 'n/a'} pt_cap={pt_cap if args.mode == 'passthrough' else 'n/a'} "
        f"echo_lead={float(args.echo_base_lead) if args.mode == 'echo' else 'n/a'} "
        f"echo_nh={int(args.echo_heavy_count) if args.mode == 'echo' else 'n/a'} "
        f"stop=Ctrl+C o «Stop drag»",
        flush=True,
    )

    smooth_buf = [0.0] * 7
    q_filt = [float(x) for x in cur]
    q_out = [float(x) for x in cur]
    prev_fb_echo: list[float] | None = None
    prev_diag_fb: list[float] | None = None
    log_next = time.monotonic() + max(0.2, float(args.log_interval_s))
    tick_count = 0
    clip_window = [0] * 7

    try:
        while not stop.is_set() and time.monotonic() < end_t:
            t0 = time.monotonic()
            with lock:
                q_now = slot[0]
            if q_now is None:
                time.sleep(period)
                continue

            if args.mode == "mirror":
                for i in range(7):
                    err = float(q_now[i]) - q_cmd[i]
                    eta_i = eta * (base_eta_mul if i < n_base else 1.0)
                    step = eta_i * err
                    cap = mcap * (base_cap_mul if i < n_base else 1.0) * (gms if i == 6 else 1.0)
                    raw_step = step
                    step = max(-cap, min(cap, step))
                    if abs(raw_step) >= cap * 0.995:
                        clip_window[i] += 1
                    q_cmd[i] += step
                cmd_q = [round(q_cmd[i], 3) for i in range(7)]
                tick_count += 1
            elif args.mode == "passthrough":
                for i in range(7):
                    q_filt[i] = pt_alpha * float(q_now[i]) + (1.0 - pt_alpha) * q_filt[i]
                    step = q_filt[i] - q_out[i]
                    step = max(-pt_cap, min(pt_cap, step))
                    q_out[i] += step
                cmd_q = [round(q_out[i], 3) for i in range(7)]
                tick_count += 1
            elif args.mode == "echo":
                nh = max(0, min(7, int(args.echo_heavy_count)))
                lg = max(0.0, float(args.echo_base_lead))
                lcap = max(0.05, float(args.echo_lead_cap_deg))
                dh = max(2, min(8, int(args.echo_decimals_heavy)))
                dr = max(2, min(8, int(args.echo_decimals_rest)))
                cmd_q = []
                for i in range(7):
                    v = float(q_now[i])
                    if lg > 0.0 and prev_fb_echo is not None and i < nh:
                        dv = v - prev_fb_echo[i]
                        if dv != 0.0:
                            step_lead = lg * dv
                            step_lead = max(-lcap, min(lcap, step_lead))
                            v += step_lead
                    dec = dh if i < nh else dr
                    cmd_q.append(round(v, dec))
                prev_fb_echo = [float(q_now[j]) for j in range(7)]
                tick_count += 1
            else:
                raw_d = [q_now[i] - prev[i] for i in range(7)]
                sm = float(args.smooth)
                sm = max(0.0, min(1.0, sm))
                for i in range(7):
                    smooth_buf[i] = sm * raw_d[i] + (1.0 - sm) * smooth_buf[i]
                delta = smooth_buf
                dg = max(abs(d) for d in delta[:6])
                if dg < float(args.deadband_deg):
                    smooth_buf[:] = [0.0] * 7
                    time.sleep(max(0.0, period - (time.monotonic() - t0)))
                    prev = [float(x) for x in q_now]
                    continue

                cmd_q = []
                for i in range(7):
                    raw_step = float(args.gain) * delta[i]
                    if i == 6:
                        raw_step *= 0.35
                    step = max(-args.max_step_deg, min(args.max_step_deg, raw_step))
                    cmd_q.append(round(q_now[i] + step, 3))

            angles = {f"angle{i}": cmd_q[i] for i in range(7)}
            angles["mode"] = 1
            out = {"seq": seq_counter, "address": 1, "funcode": 2, "data": angles}
            cmd_proc.stdin.write(json.dumps(out, separators=(",", ":")) + "\n")
            cmd_proc.stdin.flush()
            seq_counter += 1

            prev = [float(x) for x in q_now]

            if log_fp is not None and args.mode == "mirror" and time.monotonic() >= log_next:
                errs = [abs(float(q_now[i]) - q_cmd[i]) for i in range(7)]
                j_max = max(range(7), key=lambda i: errs[i])
                mean_e = sum(errs) / 7.0
                clips = [round(clip_window[i] / max(1, tick_count), 3) for i in range(7)]
                log_fp.write(
                    f"{time.strftime('%H:%M:%S')} tick={tick_count} "
                    f"max_err={errs[j_max]:.3f}°@j{j_max} mean_err={mean_e:.3f}° "
                    f"clip_rate={clips} period_ms={1000.0 * (time.monotonic() - t0):.1f}\n"
                )
                log_fp.flush()
                tc_snap = tick_count
                if diag_jsonl_fp is not None:
                    prev_diag_fb = _drag_follow_diag_jsonl_append(
                        diag_jsonl_fp,
                        mode="mirror",
                        q_now=[float(q_now[i]) for i in range(7)],
                        cmd_q=list(cmd_q),
                        tick_count=tc_snap,
                        period_ms=1000.0 * (time.monotonic() - t0),
                        prev_fb=prev_diag_fb,
                    )
                clip_window = [0] * 7
                tick_count = 0
                log_next = time.monotonic() + max(0.2, float(args.log_interval_s))
            elif log_fp is not None and args.mode == "passthrough" and time.monotonic() >= log_next:
                errs = [abs(float(q_now[i]) - q_out[i]) for i in range(7)]
                j_max = max(range(7), key=lambda i: errs[i])
                mean_e = sum(errs) / 7.0
                log_fp.write(
                    f"{time.strftime('%H:%M:%S')} tick={tick_count} passthrough "
                    f"max_err={errs[j_max]:.3f}°@j{j_max} mean_err={mean_e:.3f}° "
                    f"period_ms={1000.0 * (time.monotonic() - t0):.1f}\n"
                )
                log_fp.flush()
                tc_snap = tick_count
                if diag_jsonl_fp is not None:
                    prev_diag_fb = _drag_follow_diag_jsonl_append(
                        diag_jsonl_fp,
                        mode="passthrough",
                        q_now=[float(q_now[i]) for i in range(7)],
                        cmd_q=list(cmd_q),
                        tick_count=tc_snap,
                        period_ms=1000.0 * (time.monotonic() - t0),
                        prev_fb=prev_diag_fb,
                    )
                tick_count = 0
                log_next = time.monotonic() + max(0.2, float(args.log_interval_s))
            elif log_fp is not None and args.mode == "echo" and time.monotonic() >= log_next:
                errs = [abs(float(q_now[i]) - cmd_q[i]) for i in range(7)]
                j_max = max(range(7), key=lambda i: errs[i])
                mean_e = sum(errs) / 7.0
                log_fp.write(
                    f"{time.strftime('%H:%M:%S')} tick={tick_count} echo "
                    f"max_err={errs[j_max]:.4f}°@j{j_max} mean_err={mean_e:.4f}° "
                    f"period_ms={1000.0 * (time.monotonic() - t0):.1f}\n"
                )
                log_fp.flush()
                tc_snap = tick_count
                if diag_jsonl_fp is not None:
                    prev_diag_fb = _drag_follow_diag_jsonl_append(
                        diag_jsonl_fp,
                        mode="echo",
                        q_now=[float(q_now[i]) for i in range(7)],
                        cmd_q=list(cmd_q),
                        tick_count=tc_snap,
                        period_ms=1000.0 * (time.monotonic() - t0),
                        prev_fb=prev_diag_fb,
                    )
                tick_count = 0
                log_next = time.monotonic() + max(0.2, float(args.log_interval_s))

            elapsed = time.monotonic() - t0
            sleep_left = period - elapsed
            if sleep_left > 0:
                time.sleep(sleep_left)
    finally:
        if log_fp is not None:
            try:
                log_fp.write(f"# drag_follow end {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
                log_fp.close()
            except Exception:
                pass
        if diag_jsonl_fp is not None:
            try:
                diag_jsonl_fp.write(
                    json.dumps(
                        {"event": "session_end", "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                diag_jsonl_fp.close()
            except Exception:
                pass
        try:
            cmd_proc.stdin.close()
        except Exception:
            pass
        cmd_proc.terminate()
        try:
            cmd_proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            cmd_proc.kill()
        fb_proc.terminate()
        try:
            fb_proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            fb_proc.kill()

    print("drag_follow exit", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

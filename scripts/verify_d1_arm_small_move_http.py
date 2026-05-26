#!/usr/bin/env python3
"""
Test leggero braccio D1 via dashboard HTTP (da PC sulla LAN o sulla NX con localhost).

1) Snapshot PRIMA (tutti j0..j6)
2) Thread di campionamento: GET /api/arm/servo_snapshot?diag=1 a frequenza fissa:
   ``--samples-per-second 5`` (5 campioni/s = periodo 0.2 s) oppure ``--sample-period-s``.
3) Subito dopo il POST: snapshot DOPO_IMMEDIATO + delta vs PRIMA.
4) Il thread continua per ``--watch-s`` s dopo la fine del POST.

Uso::
  python scripts/verify_d1_arm_small_move_http.py http://192.168.123.18:5052 --samples-per-second 5 --watch-s 15

Exit: 0 OK, 1 falliti, 3 drift eccessivo dopo DOPO (vs snapshot immediato post-comando).
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

import urllib.error
import urllib.request


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _req_json(url: str, *, data: dict | None = None, timeout: float = 60.0) -> dict:
    if data is None:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        method = "GET"
    else:
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
            method="POST",
        )
        method = "POST"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"raw": body[:800]}
        raise RuntimeError(f"{method} {url} HTTP {exc.code}: {parsed}") from exc


def _snap_url(base: str, *, diag: bool) -> str:
    return f"{base}/api/arm/servo_snapshot" + ("?diag=1" if diag else "")


def _fmt7(vals: list[float]) -> list[float]:
    return [round(float(v), 3) for v in vals[:7]]


def _delta_per_joint(a: list[float], b: list[float]) -> list[float]:
    return [round(float(a[i]) - float(b[i]), 4) for i in range(7)]


def _print_joint_row(label: str, deg7: list[float]) -> None:
    v = _fmt7(deg7)
    hdr = " ".join(f"j{i:>3}" for i in range(7))
    nums = " ".join(f"{x:>8.3f}" for x in v)
    print(f"{label}", flush=True)
    print(f"     {hdr}", flush=True)
    print(f"     {nums}", flush=True)


def _print_delta_row(label: str, after: list[float], before: list[float]) -> None:
    d = _delta_per_joint(after, before)
    hdr = " ".join(f"j{i:>3}" for i in range(7))
    nums = " ".join(f"{x:>+8.3f}" for x in d)
    print(f"{label} (dopo - prima)", flush=True)
    print(f"     {hdr}", flush=True)
    print(f"     {nums}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url", nargs="?", default="http://127.0.0.1:5052", help="Dashboard base")
    ap.add_argument("--joint", type=int, default=4, help="joint_index 0..6")
    ap.add_argument("--delta-deg", type=float, default=5.0)
    ap.add_argument(
        "--sample-period-s",
        type=float,
        default=1.0,
        help="intervallo tra letture giunti (default 1 s); ignorato se è impostato --samples-per-second",
    )
    ap.add_argument(
        "--samples-per-second",
        type=float,
        default=None,
        metavar="N",
        help="frequenza campioni (es. 5 = cinque letture al secondo, periodo 0.2 s); va bene anche durante move_one",
    )
    ap.add_argument(
        "--watch-s",
        type=float,
        default=15.0,
        help="dopo il POST, quanti secondi continua il campionamento (default 15)",
    )
    ap.add_argument("--max-drift-deg", type=float, default=15.0, help="allarme drift vs snapshot DOPO immediato")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--no-diag", action="store_true", help="snapshot senza ?diag=1")
    ap.add_argument("--log-file", type=str, default="", help="append NDJSON una riga per campione")
    ap.add_argument(
        "--fail-on-stale",
        action="store_true",
        help="exit 4 se troppe letture AFTER uguali (feedback stagnante)",
    )
    ap.add_argument("--stale-eps-deg", type=float, default=0.02)
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    use_diag = not args.no_diag
    if args.samples_per_second is not None:
        rate = float(args.samples_per_second)
        if rate <= 0:
            print("--samples-per-second must be > 0", file=sys.stderr)
            return 1
        period_s = 1.0 / rate
    else:
        period_s = float(args.sample_period_s)
    period_s = max(0.05, min(period_s, 30.0))

    def log(line: str) -> None:
        print(line, flush=True)

    def log_nd(payload: dict[str, Any]) -> None:
        payload = dict(payload)
        payload["ts"] = _utc_iso()
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if args.log_file:
            with open(args.log_file, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
        if args.verbose:
            log(f"NDJSON {line}")

    snap_url = _snap_url(base, diag=use_diag)

    t0 = time.monotonic()
    snap = _req_json(snap_url, timeout=30.0)
    log(f"[{_utc_iso()}] baseline_dt_ms={(time.monotonic()-t0)*1000:.0f}")
    if args.verbose:
        log(json.dumps(snap, indent=2, ensure_ascii=False))

    if not snap.get("ok"):
        print("SERVO_SNAPSHOT_FAIL " + json.dumps(snap, ensure_ascii=False), file=sys.stderr)
        return 1

    cur = [float(x) for x in snap["servo_deg"][:7]]
    diag_b = snap.get("servo_feedback_diag") or {}
    log("")
    log("=== PRIMA del comando (tutti i giunti, snapshot DDS) ===")
    _print_joint_row("PRIMA", cur)
    log(
        f"(diag) backend={diag_b.get('backend')} lines={diag_b.get('servo_angle_lines')} "
        f"spread_max_deg={diag_b.get('servo_max_spread_any_joint_deg')}"
    )
    log("")
    log_nd({"phase": "baseline", "servo_deg": _fmt7(cur), "servo_feedback_diag": diag_b})

    if args.dry_run:
        log("dry-run ok")
        return 0

    ji = max(0, min(6, int(args.joint)))
    log("(nota lab) Spesso: pinza=j6, polso vicino pinza=j5, altro asse polso=j4.")
    delta = float(args.delta_deg)
    target = round(cur[ji] + delta, 3)
    target = max(-130.0, min(130.0, target))
    log(
        f"COMANDO move_one: joint_index={ji}  da {cur[ji]:.3f} deg -> {target:.3f} deg "
        f"(delta sul giunto {ji}: {target - cur[ji]:+.3f} deg)"
    )
    log(
        f"Campionamento ~{1.0 / period_s:.4g} Hz (ogni {period_s:.3f} s) durante il POST e ancora {float(args.watch_s):.1f} s dopo la fine del POST."
    )
    log("")

    move_done = threading.Event()
    t_post_done: dict[str, float | None] = {"v": None}
    immediate_post: dict[str, list[float] | None] = {"v": None}
    sampler_stop = threading.Event()
    max_drift_vs_dopo = {"v": 0.0}
    worst_j_vs_dopo = {"v": 0}
    stale_after = {"n": 0}
    prev_after: dict[str, list[float] | None] = {"v": None}

    t_series_start = time.monotonic()

    def sampler_loop() -> None:
        idx = 0
        while not sampler_stop.is_set():
            idx += 1
            t_el = time.monotonic() - t_series_start
            stage = "DURING_MOVE" if not move_done.is_set() else "AFTER_MOVE"
            try:
                s = _req_json(snap_url, timeout=30.0)
            except Exception as exc:
                log(f"SAMPLE#{idx} FAIL ({stage}) t={t_el:.2f}s {exc!r}")
                log_nd({"phase": "sample_fail", "n": idx, "stage": stage, "elapsed_s": round(t_el, 3), "err": repr(exc)})
                time.sleep(period_s)
                if move_done.is_set() and t_post_done["v"] is not None:
                    if time.monotonic() - t_post_done["v"] >= float(args.watch_s):
                        break
                continue

            if not s.get("ok"):
                log(f"SAMPLE#{idx} no_ok ({stage}) t={t_el:.2f}s {json.dumps(s)[:320]}")
                log_nd({"phase": "sample_no_ok", "n": idx, "stage": stage, "resp": s})
                time.sleep(period_s)
                if move_done.is_set() and t_post_done["v"] is not None:
                    if time.monotonic() - t_post_done["v"] >= float(args.watch_s):
                        break
                continue

            q = [float(x) for x in s["servo_deg"][:7]]
            dg = s.get("servo_feedback_diag") or {}
            dvb = _delta_per_joint(q, cur)
            mx_b = max(abs(x) for x in dvb)

            ip = immediate_post["v"]
            extra_dopo = ""
            if ip is not None and stage == "AFTER_MOVE":
                dvp = [abs(q[j] - ip[j]) for j in range(7)]
                mx_p = max(dvp)
                wj = max(range(7), key=lambda j: dvp[j])
                if mx_p > max_drift_vs_dopo["v"]:
                    max_drift_vs_dopo["v"] = mx_p
                    worst_j_vs_dopo["v"] = wj
                extra_dopo = f" maxAbs_vs_DOPO_IMMEDIATO={mx_p:.3f} @j{wj}"

                pv = prev_after["v"]
                if pv is not None:
                    if all(abs(q[j] - pv[j]) < float(args.stale_eps_deg) for j in range(7)):
                        stale_after["n"] += 1
                prev_after["v"] = list(q)

            log("")
            _print_joint_row(f"SAMPLE#{idx} t={t_el:.2f}s {stage}", q)
            _print_delta_row("  DELTA_vs_PRIMA", q, cur)
            log(
                f"  maxAbs_vs_PRIMA={mx_b:.3f}{extra_dopo} | intra_read_spread={dg.get('servo_max_spread_any_joint_deg')} lines={dg.get('servo_angle_lines')}"
            )
            log_nd(
                {
                    "phase": "sample",
                    "n": idx,
                    "elapsed_since_series_start_s": round(t_el, 3),
                    "stage": stage,
                    "servo_deg": _fmt7(q),
                    "delta_vs_prima": dvb,
                    "servo_feedback_diag": dg,
                }
            )
            if args.verbose:
                log(json.dumps(s, indent=2, ensure_ascii=False)[:4000])

            time.sleep(period_s)

            if move_done.is_set() and t_post_done["v"] is not None:
                if time.monotonic() - t_post_done["v"] >= float(args.watch_s):
                    break

    th = threading.Thread(target=sampler_loop, name="servo-sampler", daemon=True)
    th.start()

    t_move = time.monotonic()
    try:
        move = _req_json(
            f"{base}/api/arm/joints/move_one",
            data={"joint_index": ji, "angle_deg": target},
            timeout=180.0,
        )
    finally:
        move_done.set()
        t_post_done["v"] = time.monotonic()

    log(f"[{_utc_iso()}] move_one_wall_ms={(time.monotonic()-t_move)*1000:.0f}")
    ok = bool(move.get("ok")) or bool(move.get("skipped"))
    log(f"move_one ok={move.get('ok')} skipped={move.get('skipped')} reason={move.get('reason')}")
    log_nd({"phase": "move_response", "body": move})
    if args.verbose:
        log(json.dumps(move, indent=2, ensure_ascii=False)[:12000])
    if not ok:
        sampler_stop.set()
        th.join(timeout=5.0)
        print(json.dumps(move, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1

    t_snap = time.monotonic()
    post = _req_json(snap_url, timeout=30.0)
    log(f"[{_utc_iso()}] post_move_snap_dt_ms={(time.monotonic()-t_snap)*1000:.0f}")
    if not post.get("ok"):
        sampler_stop.set()
        th.join(timeout=5.0)
        print("POST_MOVE_SNAPSHOT_FAIL " + json.dumps(post, ensure_ascii=False), file=sys.stderr)
        return 1

    pos = [float(x) for x in post["servo_deg"][:7]]
    immediate_post["v"] = pos
    prev_after["v"] = list(pos)
    diag_p = post.get("servo_feedback_diag") or {}
    cmd_vs_fb_ji = round(pos[ji] - target, 4)

    log("")
    log("=== SUBITO DOPO il comando (snapshot DDS, stesso momento logico del thread AFTER) ===")
    _print_joint_row("DOPO_IMMEDIATO", pos)
    _print_delta_row("DELTA vs PRIMA", pos, cur)
    log(
        f"Errore giunto comandato j{ji}: feedback - comando = {cmd_vs_fb_ji:.4f} deg | "
        f"(diag) lines={diag_p.get('servo_angle_lines')} spread={diag_p.get('servo_max_spread_any_joint_deg')}"
    )
    log("")
    log_nd({"phase": "post_move", "servo_deg": _fmt7(pos), "servo_feedback_diag": diag_p, "joint_command_err_deg": cmd_vs_fb_ji})

    join_timeout = float(args.watch_s) + period_s * 4 + 45.0
    th.join(timeout=join_timeout)
    sampler_stop.set()
    th.join(timeout=3.0)

    log("")
    log(
        f"[riepilogo] Campionamento ogni {period_s:.2f}s durante move + {float(args.watch_s):.1f}s dopo POST. "
        f"Max drift vs DOPO_IMMEDIATO (solo fase AFTER_MOVE): {max_drift_vs_dopo['v']:.3f} deg (worst j{worst_j_vs_dopo['v']}) "
        f"| stale_after_reads={stale_after['n']}"
    )
    thr = float(args.max_drift_deg)
    if max_drift_vs_dopo["v"] > thr:
        print(f"FAIL drift vs DOPO_IMMEDIATO > {thr} deg — possibile caduta/instabilità dopo comando", file=sys.stderr)
        return 3
    if args.fail_on_stale and stale_after["n"] >= max(4, int(float(args.watch_s) / period_s * 0.5)):
        print(
            f"FAIL stale: {stale_after['n']} letture AFTER quasi identiche — feedback potrebbe non aggiornarsi",
            file=sys.stderr,
        )
        return 4
    log(f"OK drift vs DOPO_IMMEDIATO <= {thr} deg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Giro completo laboratorio: preflight → camera polso → teach grasp.

Esempio (PC sulla LAN Unitree 192.168.123.x):

  python scripts/lab/lab_grasp_system_round.py
  python scripts/lab/lab_grasp_system_round.py --base http://192.168.123.18:5052 --wait 180
  python scripts/lab/lab_grasp_system_round.py --dry-run   # solo controlli, no movimento

Prima del ciclo reale: scatola visibile al polso, cane fermo, area libera davanti al gripper.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_DBG_LOG = Path(__file__).resolve().parents[2] / "debug-fdb211.log"


def _dbg(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict | None = None,
    *,
    run_id: str = "lab-round",
) -> None:
    # #region agent log
    try:
        with _DBG_LOG.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "sessionId": "fdb211",
                        "hypothesisId": hypothesis_id,
                        "location": location,
                        "message": message,
                        "data": data or {},
                        "timestamp": int(time.time() * 1000),
                        "runId": run_id,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass
    # #endregion


def _get(base: str, path: str, *, timeout: float = 20.0) -> tuple[int, dict]:
    url = base + path
    req = urllib.request.Request(url, method="GET", headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return int(resp.getcode() or 200), json.loads(raw) if raw.strip() else {}


def _post(base: str, path: str, body: dict | None = None, *, timeout: float = 45.0) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return int(resp.getcode() or 200), json.loads(raw) if raw.strip() else {}


def _wait_reachable(base: str, wait_s: float) -> bool:
    deadline = time.time() + max(0.0, wait_s)
    while time.time() < deadline:
        try:
            code, body = _get(base, "/api/health", timeout=5.0)
            if code == 200 and body.get("ok"):
                print(f"OK dashboard raggiungibile (pid={body.get('pid')})")
                return True
        except Exception as exc:
            print(f"  attesa NX… ({exc})")
        time.sleep(3.0)
    return False


def _print_section(title: str) -> None:
    print(f"\n=== {title} ===")


def _preflight(base: str, *, force_usb2: bool) -> bool:
    ok = True

    _print_section("HEALTH")
    try:
        code, health = _get(base, "/api/health")
        print(json.dumps({k: health.get(k) for k in ("ok", "service", "pid", "go2_local", "operator_dashboard")}, ensure_ascii=False))
        if not health.get("ok"):
            ok = False
    except Exception as exc:
        print(f"FAIL health: {exc}")
        return False

    _print_section("MISSION CONSOLE")
    try:
        code, mc = _get(base, "/api/mission/console", timeout=30.0)
        env = mc.get("env") or {}
        summary = mc.get("summary") or {}
        print(
            "GO2_ENABLE_REAL_ARM=",
            env.get("GO2_ENABLE_REAL_ARM"),
            " GO2_WRIST_DEPTH_BACKEND=",
            env.get("GO2_WRIST_DEPTH_BACKEND"),
        )
        print("dashboard_pid=", summary.get("dashboard_pid"))
        if str(env.get("GO2_ENABLE_REAL_ARM", "")).strip() not in ("1", "true", "yes", "on"):
            print("WARN: GO2_ENABLE_REAL_ARM non attivo — il braccio non si muoverà.")
    except Exception as exc:
        print(f"WARN mission/console: {exc}")

    _print_section("CAMERE USB")
    try:
        code, cam = _get(base, "/api/cameras/status", timeout=25.0)
        print("auto_map:", cam.get("v4l_usb_auto_map"))
        print("index_by_logical:", cam.get("v4l_index_by_logical"))
        inv = cam.get("v4l_usb_inventory") or []
        for row in inv:
            if str(row.get("usb_vid_pid", "")).startswith("8086:"):
                print(
                    f"  video{row.get('v4l_index')}: {row.get('usb_vid_pid')} "
                    f"logical={row.get('logical')} {row.get('name', '')}"
                )
    except Exception as exc:
        print(f"WARN cameras/status: {exc}")

    _print_section("CAMERA POLSO (depth)")
    try:
        code, wh = _get(base, "/api/grasp/wrist_camera_health", timeout=35.0)
        print(json.dumps(
            {k: wh.get(k) for k in ("ok", "reason", "usb_type", "serial", "depth_center_median_m", "hint_it")},
            ensure_ascii=False,
            indent=2,
        ))
        _dbg("H1", "lab_grasp_system_round.py:preflight", "wrist_camera_health", {"http": code, **{k: wh.get(k) for k in ("ok", "reason", "usb_type")}})
        if not wh.get("ok"):
            ok = False
            if wh.get("reason") == "usb2_low_bandwidth" and not force_usb2:
                print("BLOCCO: D456 su USB 2.x — sposta su USB 3.0 Type-A della docking Go2 (o patch DTB).")
                print("      Usa --force-usb2 solo per test senza depth reale.")
    except Exception as exc:
        print(f"FAIL wrist_camera_health: {exc}")
        ok = False

    _print_section("TEACH / AUTONOMOUS STATUS")
    for path in ("/api/grasp/teach_status", "/api/grasp/autonomous_status"):
        try:
            _, st = _get(base, path)
            print(
                path,
                json.dumps(
                    {k: st.get(k) for k in ("running", "ok", "current_step", "label_it", "progress_pct")},
                    ensure_ascii=False,
                ),
            )
        except Exception as exc:
            print(path, "ERR", exc)

    return ok


def _run_teach_cycle(base: str, instruction: str, max_cycles: int, timeout_s: int) -> int:
    _print_section("CANCEL PREFLIGHT")
    try:
        _, cancel = _post(base, "/api/grasp/teach_cancel", {"reason_it": "reset lab_grasp_system_round"})
        print("was_running=", cancel.get("was_running"), "cancelled=", cancel.get("cancelled"))
    except Exception as exc:
        print(f"WARN teach_cancel: {exc}")

    _print_section("AVVIO TEACH RUN")
    try:
        code, body = _post(
            base,
            "/api/grasp/teach_run",
            {
                "confirm": "RUN_TEACH_GRASP",
                "instruction": instruction,
                "max_cycles": max_cycles,
            },
            timeout=60.0,
        )
    except urllib.error.HTTPError as he:
        raw = he.read().decode("utf-8", errors="replace")
        print(f"HTTP {he.code}", raw[:800])
        return 2

    print(
        "HTTP",
        code,
        "started=",
        body.get("started"),
        "mode=",
        body.get("mode"),
        "reason=",
        body.get("reason"),
    )
    _dbg(
        "H2",
        "lab_grasp_system_round.py:teach_run",
        "teach_run_response",
        {"http": code, "started": body.get("started"), "reason": body.get("reason"), "mode": body.get("mode")},
    )
    if not body.get("started"):
        print("FAIL avvio:", json.dumps(body, ensure_ascii=False)[:1200])
        return 2

    deadline = time.time() + timeout_s
    last_step = None
    last_pct = -1

    _print_section("MONITORAGGIO CICLO")
    while time.time() < deadline:
        st = _get(base, "/api/grasp/teach_status")[1]
        step = st.get("current_step")
        pct = st.get("progress_pct")
        running = st.get("running")
        if step != last_step or pct != last_pct:
            print(
                f"  [{time.strftime('%H:%M:%S')}] running={running} step={step} "
                f"pct={pct} ok={st.get('ok')} — {st.get('label_it')}"
            )
            _dbg(
                "H5",
                "lab_grasp_system_round.py:teach_status",
                "status_tick",
                {
                    "step": step,
                    "pct": pct,
                    "running": running,
                    "ok": st.get("ok"),
                    "failed_step": st.get("failed_step"),
                    "label_it": st.get("label_it"),
                },
            )
            lines = st.get("log_lines") or []
            if lines:
                print("    log:", lines[-1].get("msg_it", ""))
            last_step, last_pct = step, pct

        if not running:
            _print_section("ESITO FINALE")
            print("ok=", st.get("ok"), "failed_step=", st.get("failed_step"))
            print("label=", st.get("label_it"))
            for s in st.get("steps") or []:
                print(f"  {s.get('id')}: {s.get('status')} — {s.get('detail', '')}")
            if st.get("grasp_verify"):
                print("grasp_verify:", json.dumps(st.get("grasp_verify"), ensure_ascii=False)[:500])
            if st.get("ok"):
                print("LAB_GRASP_SYSTEM_ROUND_OK")
                _dbg("H5", "lab_grasp_system_round.py:final", "cycle_ok", {"failed_step": st.get("failed_step")})
                return 0
            print("LAB_GRASP_SYSTEM_ROUND_FAIL")
            _dbg(
                "H5",
                "lab_grasp_system_round.py:final",
                "cycle_fail",
                {
                    "failed_step": st.get("failed_step"),
                    "steps": st.get("steps"),
                    "label_it": st.get("label_it"),
                },
            )
            return 3

        time.sleep(2.0)

    print(f"TIMEOUT {timeout_s}s")
    return 4


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default="http://192.168.123.18:5052", help="URL dashboard NX")
    p.add_argument("--wait", type=float, default=0.0, help="Secondi di attesa finché la NX risponde")
    p.add_argument("--instruction", default="prendi la scatola", help="Istruzione teach")
    p.add_argument("--max-cycles", type=int, default=3)
    p.add_argument("--timeout", type=int, default=600, help="Timeout monitoraggio ciclo (s)")
    p.add_argument("--dry-run", action="store_true", help="Solo preflight, nessun movimento")
    p.add_argument(
        "--force-usb2",
        action="store_true",
        help="Procedi anche se la D456 è su USB 2.x (depth quasi vuota)",
    )
    args = p.parse_args()
    base = args.base.rstrip("/")

    print(f"Target: {base}")
    if args.wait > 0 and not _wait_reachable(base, args.wait):
        print("NX non raggiungibile — collega il PC alla LAN 192.168.123.x (cavo verso il Go2).")
        return 10

    try:
        _get(base, "/api/health", timeout=8.0)
    except Exception as exc:
        print(f"NX non raggiungibile: {exc}")
        print("Collega il PC alla rete del robot (192.168.123.x) e rilancia.")
        return 10

    if not _preflight(base, force_usb2=args.force_usb2):
        if not args.force_usb2:
            return 11

    if args.dry_run:
        print("\nDRY-RUN: preflight OK, ciclo teach non avviato.")
        return 0

    return _run_teach_cycle(base, args.instruction, args.max_cycles, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())

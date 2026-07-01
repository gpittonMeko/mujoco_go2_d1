#!/usr/bin/env python3
"""Test grasp teach con dump completo log, gate, camere, coach."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request


def _get(base: str, path: str) -> dict:
    req = urllib.request.Request(base + path, method="GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}


def _post(base: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return resp.status, json.loads(raw) if raw.strip() else {}


def _print_section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _dump_cameras(base: str) -> None:
    _print_section("CAMERE (UVC log.0 / log.6 — indipendenti da SDK Orbbec)")
    try:
        cam = _get(base, "/api/cameras/status")
    except Exception as exc:
        print("ERR cameras/status:", exc)
        return
    for idx in ("0", "6"):
        s = (cam.get("camera_summary") or {}).get(idx) or {}
        print(
            f"  log.{idx}: device={s.get('device_path')} "
            f"name={s.get('card_name') or s.get('name')} color_ok={s.get('color_ok')}"
        )
    s0 = (cam.get("camera_summary") or {}).get("0") or {}
    s6 = (cam.get("camera_summary") or {}).get("6") or {}
    if s0.get("device_path") and s0.get("device_path") == s6.get("device_path"):
        print("  WARN: log.0 e log.6 stesso device — UVC preview confusa, SDK Orbbec puo essere OK lo stesso")
    print("  NOTA: la presa metrica usa SDK Orbbec (pannello centrale), non log.0 UVC.")


def _dump_coach(base: str) -> None:
    _print_section("COACH / GPT SUPERVISOR")
    try:
        c = _get(base, "/api/grasp_coach/status")
    except Exception as exc:
        print("ERR:", exc)
        return
    print("  enabled:", c.get("enabled"))
    print("  supervisor_enabled:", c.get("supervisor_enabled"))
    print("  model:", c.get("model"))
    if not c.get("enabled"):
        print("  -> GPT supervisor SALTATO (GO2_ENABLE_GRASP_COACH=0 o no API key)")


def _dump_full_status(st: dict) -> None:
    _print_section("STATO FINALE teach_status")
    for k in (
        "running",
        "ok",
        "mode",
        "current_step",
        "failed_step",
        "progress_pct",
        "label_it",
        "started_at",
        "finished_at",
    ):
        print(f"  {k}: {st.get(k)}")
    print("\n  STEPS:")
    for s in st.get("steps") or []:
        print(f"    [{s.get('status')}] {s.get('id')}: {s.get('detail', '')}")
    gates = st.get("gates")
    if isinstance(gates, dict):
        print("\n  GATES:", json.dumps(gates, ensure_ascii=False))
    mp = st.get("metric_plan")
    if isinstance(mp, dict):
        det = mp.get("object_detection") if isinstance(mp.get("object_detection"), dict) else {}
        print("\n  METRIC_PLAN:")
        print("    ok:", mp.get("ok"), "reason:", mp.get("reason"))
        print("    depth_m:", mp.get("depth_m"), "reachable:", mp.get("reachable"))
        print("    detect ok:", det.get("ok"), "conf:", det.get("confidence"), "color:", det.get("color_hint"))
    gv = st.get("grasp_verify")
    if isinstance(gv, dict):
        print("\n  GRASP_VERIFY:", json.dumps(gv, ensure_ascii=False)[:500])
    print("\n  LOG COMPLETO:")
    for ln in st.get("log_lines") or []:
        print(f"    [{ln.get('level','?')}] {ln.get('step','')}: {ln.get('msg_it','')}")


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://192.168.123.18:5052").rstrip("/")
    instruction = sys.argv[2] if len(sys.argv) > 2 else "prendi la scatola blu"
    timeout_s = int(sys.argv[3]) if len(sys.argv) > 3 else 420

    _dump_cameras(base)
    _dump_coach(base)

    _print_section("PREFLIGHT teach_cancel")
    _, cancel = _post(base, "/api/grasp/teach_cancel", {"reason_it": "reset verbose test"})
    print("  was_running:", cancel.get("was_running"), "cancelled:", cancel.get("cancelled"))

    _print_section("AVVIO teach_run")
    t0 = time.time()
    try:
        code, body = _post(
            base,
            "/api/grasp/teach_run",
            {
                "confirm": "RUN_TEACH_GRASP",
                "instruction": instruction,
                "max_cycles": 5,
            },
        )
    except urllib.error.HTTPError as he:
        print("HTTP ERROR", he.code, he.read().decode("utf-8", errors="replace")[:600])
        return 1
    except Exception as exc:
        print("POST FAIL", exc)
        return 1
    print(f"  elapsed={time.time()-t0:.2f}s HTTP={code} started={body.get('started')} reason={body.get('reason')}")
    if not body.get("started"):
        print(json.dumps(body, ensure_ascii=False, indent=2)[:1200])
        return 2

    _print_section("POLLING teach_status")
    deadline = time.time() + timeout_s
    last_key = None
    while time.time() < deadline:
        st = _get(base, "/api/grasp/teach_status")
        key = (st.get("current_step"), st.get("progress_pct"), st.get("label_it"), len(st.get("log_lines") or []))
        if key != last_key:
            print(
                f"  [{time.strftime('%H:%M:%S')}] step={st.get('current_step')} "
                f"pct={st.get('progress_pct')} running={st.get('running')} | {st.get('label_it')}"
            )
            for ln in (st.get("log_lines") or [])[-2:]:
                print(f"      log [{ln.get('level')}]: {ln.get('msg_it')}")
            last_key = key
        if not st.get("running"):
            _dump_full_status(st)
            if st.get("ok"):
                print("\nRESULT: GRASP_TEACH_OK")
                return 0
            print("\nRESULT: GRASP_TEACH_FAIL")
            return 3
        time.sleep(1.5)

    st = _get(base, "/api/grasp/teach_status")
    _dump_full_status(st)
    print("\nRESULT: TIMEOUT")
    return 4


if __name__ == "__main__":
    raise SystemExit(main())

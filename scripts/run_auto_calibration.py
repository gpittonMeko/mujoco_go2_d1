#!/usr/bin/env python3
"""Driver AUTO hand-eye 6D: chiama /auto_step in loop fino a cal_ok.

Uso: python scripts/run_auto_calibration.py [host] [max_steps]
Monitora l'avanzamento, tiene solo i sample che migliorano il residuo
(gestito lato server), e stampa lo stato ad ogni step.
"""
from __future__ import annotations

import functools
import json
import sys
import time
import urllib.request

print = functools.partial(print, flush=True)  # noqa: A001

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.123.18:5056"
MAX_STEPS = int(sys.argv[2]) if len(sys.argv) > 2 else 60
BASE = f"http://{HOST}"


def _post(path: str, body: dict, timeout: float = 200.0) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        try:
            return {**json.loads(e.read().decode()), "_http": e.code}
        except Exception:
            return {"ok": False, "reason": f"http_{e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"exc:{e!r}"}


def _get(path: str, timeout: float = 15.0) -> dict:
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"exc:{e!r}"}


def _res_txt(q: dict | None) -> str:
    q = q or {}
    r = q.get("residual") if isinstance(q.get("residual"), dict) else {}
    tm = r.get("translation_rms_m")
    rd = r.get("rotation_rms_deg")
    if tm is None or rd is None:
        return "residuo n/d"
    return f"{float(tm) * 100:.1f}cm / {float(rd):.1f}deg"


def main() -> int:
    print(f"[driver] AUTO hand-eye -> {BASE}  max_steps={MAX_STEPS}")
    soft_reasons = {
        "search_viewpoint", "pose_too_similar", "residual_not_improving",
        "too_few_visible_tags", "reprojection_too_high", "target_not_valid",
        "marker_not_visible", "aprilgrid_corner_pose_required",
        "aprilgrid_not_enough_expected_tags", "singular_sample_transform",
        "handeye_linalg_error", "handeye_solver_failed",
        "max_samples_still_residual_high",
    }
    for step in range(MAX_STEPS):
        body = {"confirm": "AUTO_CALIBRATE_6D", "step": step, "max_samples": 16}
        if step == 0:
            body["new_session"] = True
        t0 = time.time()
        d = _post("/api/pick/metric/calibration/auto_step", body)
        dt = time.time() - t0
        saved = d.get("saved")
        reason = d.get("reason")
        sc = None
        if isinstance(d.get("sample"), dict):
            sc = d["sample"].get("sample_count")
        q = d.get("quality") or d.get("current_quality") or {}
        srch = d.get("search")
        tag = None
        if isinstance(d.get("marker"), dict):
            tag = d["marker"].get("visible_marker_count")
        tail = ""
        if srch:
            tail = f" | SEARCH {srch.get('index')}/{srch.get('total')} tags={srch.get('tags')} best={srch.get('best_tags')} done={srch.get('done')}"
        elif tag is not None:
            tail = f" | tags={tag}"
        print(f"[{step:02d}] ok={d.get('ok')} saved={saved} reason={reason} n={sc if sc is not None else '-'} {_res_txt(q)} ({dt:.1f}s){tail}")

        if d.get("done") and d.get("build") and d["build"].get("ok"):
            print(f"[driver] CAL_OK! build={_res_txt({'residual': d['build']})} sample={d['build'].get('sample_count')}")
            return 0
        if not d.get("ok") and reason not in soft_reasons:
            print(f"[driver] STOP hard error: {reason}  detail={json.dumps(d)[:400]}")
            return 2
        time.sleep(0.4)

    # Check finale
    cal = _get("/api/pick/metric/calibration")
    ok = bool((cal.get("calibration") or {}).get("ok")) and bool((cal.get("quality") or {}).get("build_ready"))
    print(f"[driver] fine loop. build_ready={ (cal.get('quality') or {}).get('build_ready') } {_res_txt(cal.get('quality'))} sample={cal.get('sample_count')}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

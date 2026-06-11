#!/usr/bin/env python3
"""
Prova presa locale senza AnyGrasp: dry-run run_full → (opz.) esecuzione lenta.

Mostra **cosa vedono le camere** (bbox + JPEG annotati):
  GET /api/grasp/detection_debug
  GET /api/grasp/detection_debug/front.jpg | wrist_orbbec.jpg
  GET /api/vision/box_detect?camera=6|0

Salva in locale (repo data/): grasp_verify_last.json + grasp_debug_*.jpg

Esempi::
  python scripts/verify_grasp_orbbec_local_http.py http://192.168.123.18:5052
  python scripts/verify_grasp_orbbec_local_http.py http://192.168.123.18:5052 --execute
  python scripts/verify_grasp_orbbec_local_http.py http://192.168.123.18:5052 --save-local
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"


def _req_json(url: str, *, data: dict | None = None, timeout: float = 180.0) -> tuple[dict, int]:
    if data is None:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    else:
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json", "Cache-Control": "no-cache"},
            method="POST",
        )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = int(resp.getcode() or 200)
            body = resp.read().decode("utf-8", errors="replace")
            return (json.loads(body) if body.strip() else {"ok": True}), code
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw.strip() else {"ok": False}
        except json.JSONDecodeError:
            parsed = {"ok": False, "raw": raw[:1200]}
        return parsed, int(exc.code)


def _fetch_bytes(url: str, timeout: float = 25.0) -> bytes | None:
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"Cache-Control": "no-cache"}),
            timeout=timeout,
        ) as resp:
            return resp.read()
    except Exception:
        return None


def _print_detection_block(title: str, det: dict | None, snap: dict | None, url: str | None) -> None:
    print(f"\n--- {title} ---")
    if snap and snap.get("image_url"):
        print(f"  immagine annotata: {url or snap.get('image_url')}")
    if not isinstance(det, dict):
        print("  NESSUNA detection nel JSON")
        return
    ok = bool(det.get("ok"))
    print(f"  detection_ok: {ok}")
    print(f"  backend: {det.get('backend')}  label: {det.get('label')}  confidence: {det.get('confidence')}")
    print(f"  reason: {det.get('reason', '')}")
    bbox = det.get("bbox_xyxy")
    if isinstance(bbox, list) and len(bbox) >= 4:
        print(f"  bbox_xyxy: [{bbox[0]:.0f}, {bbox[1]:.0f}, {bbox[2]:.0f}, {bbox[3]:.0f}]")
        center = det.get("bbox_center_px")
        if center:
            print(f"  center_px: {center}")
    else:
        print("  bbox: MANCANTE — il detector non vede un oggetto box-like")


def _box_detect_live(base: str, camera: int) -> dict:
    out, code = _req_json(f"{base}/api/vision/box_detect?camera={camera}", timeout=30)
    print(f"\n=== box_detect live camera={camera} HTTP {code} ===")
    det = out.get("detection") if isinstance(out, dict) else None
    _print_detection_block(f"Live cache cam {camera}", det, None, f"{base}/api/robot/camera/{camera}.jpg")
    return out


def _save_local_debug(base: str, manifest: dict, run_full: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "grasp_verify_last.json").write_text(
        json.dumps({"run_full": run_full, "manifest": manifest}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nLog locale: {DATA / 'grasp_verify_last.json'}")
    snaps = (manifest or {}).get("snapshots") or {}
    for tag, row in snaps.items():
        if not isinstance(row, dict):
            continue
        rel = row.get("image_url") or f"/api/grasp/detection_debug/{tag}.jpg"
        raw = _fetch_bytes(base + rel if rel.startswith("/") else f"{base}/{rel}")
        if raw:
            dest = DATA / f"grasp_debug_{tag}.jpg"
            dest.write_bytes(raw)
            print(f"  salvato {dest}")


def _step_summary(steps: list) -> list[dict]:
    out = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        row = {
            "step": s.get("step"),
            "ok": s.get("ok"),
            "skipped": s.get("skipped"),
            "reason": s.get("reason"),
            "backend": s.get("backend"),
            "metric_path": s.get("metric_path"),
            "label_it": s.get("label_it"),
        }
        dbg = s.get("debug_snapshot") or {}
        if dbg.get("detection_ok") is not None:
            row["detection_ok"] = dbg.get("detection_ok")
        if s.get("step") == "wrist_plan" and isinstance(s.get("plan"), dict):
            p = s["plan"]
            row["plan_backend"] = p.get("backend")
            ass = p.get("grasp_assessment") or {}
            row["execution_allowed"] = ass.get("execution_allowed")
            od = p.get("object_detection") or {}
            if od.get("bbox_xyxy"):
                row["wrist_bbox"] = od.get("bbox_xyxy")
        out.append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Verifica grasp locale Orbbec + log detection")
    ap.add_argument("base_url", nargs="?", default="http://192.168.123.18:5052")
    ap.add_argument("--execute", action="store_true", help="Muove braccio (confirm RUN_FULL_GRASP)")
    ap.add_argument("--save-local", action="store_true", help="Scarica JPEG annotati in data/")
    ap.add_argument(
        "--instruction",
        default="afferra l'oggetto davanti al braccio",
    )
    ap.add_argument("--skip-live-detect", action="store_true", help="Salta GET box_detect pre-run")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    if not args.skip_live_detect:
        _box_detect_live(base, 6)
        _box_detect_live(base, 0)

    print("\n=== grasp/health ===")
    health, _ = _req_json(f"{base}/api/grasp/health", timeout=20)
    print(f"  worker_reachable={health.get('worker_reachable')}")

    body: dict = {"instruction": args.instruction, "goto_start": True, "execute": True}
    if args.execute:
        body["confirm"] = "RUN_FULL_GRASP"
        print("\n=== run_full ESECUZIONE ===")
    else:
        print("\n=== run_full DRY-RUN ===")

    out, code = _req_json(f"{base}/api/grasp/run_full", data=body, timeout=600)
    print(f"HTTP {code}  ok={out.get('ok')}  failed_step={out.get('failed_step')}")

    urls = out.get("detection_debug_urls") or {}
    manifest = out.get("detection_debug") or {}
    if not manifest.get("ok"):
        manifest, _ = _req_json(f"{base}/api/grasp/detection_debug", timeout=15)

    print("\n=== DETECTION DEBUG (dopo run_full) ===")
    print(f"  manifest API: {base}/api/grasp/detection_debug")
    for key, path in urls.items():
        if path:
            print(f"  {key}: {base}{path}")

    snaps = manifest.get("snapshots") or {}
    for tag, row in snaps.items():
        if isinstance(row, dict):
            print(
                f"  [{tag}] ok={row.get('detection_ok')} backend={row.get('backend')} "
                f"conf={row.get('confidence')} bbox={row.get('bbox_xyxy')}"
            )

    for s in out.get("steps") or []:
        if not isinstance(s, dict):
            continue
        st = s.get("step")
        if st == "front_detect":
            _print_detection_block(
                "Step front_detect (cam 6)",
                s.get("detection"),
                s.get("debug_snapshot"),
                base + "/api/grasp/detection_debug/front.jpg",
            )
        elif st in ("goto_start", "start_pose_check"):
            chk = s.get("at_start_check") or s
            print(f"\n--- Step {st} ---")
            print(f"  ok={s.get('ok')} skipped={s.get('skipped')}")
            if s.get("reason"):
                print(f"  reason: {s.get('reason')}")
            if chk.get("max_error_deg") is not None:
                print(f"  max_error_deg vs START: {chk.get('max_error_deg')} (tol {chk.get('tolerance_deg')})")
            if chk.get("hint_it"):
                print(f"  hint: {chk.get('hint_it')}")
            if s.get("label_it"):
                print(f"  {s.get('label_it')}")
        elif st == "wrist_plan":
            plan = s.get("plan") if isinstance(s.get("plan"), dict) else {}
            _print_detection_block(
                "Step wrist_plan Orbbec (cam 0)",
                plan.get("object_detection") or s.get("wrist_detection"),
                plan.get("debug_snapshot") or s.get("debug_snapshot"),
                base + "/api/grasp/detection_debug/wrist_orbbec.jpg",
            )
            tgt = (plan.get("target") or {}).get("base_xyz_m")
            if tgt:
                print(f"  target base_link_m: {tgt}")
            print(f"  reachable={plan.get('reachable')} reach_m={plan.get('reach_m')}")

    if args.save_local or True:
        _save_local_debug(base, manifest if isinstance(manifest, dict) else {}, out)

    print("\n=== steps summary ===")
    print(json.dumps(_step_summary(out.get("steps") or []), indent=2, ensure_ascii=False))

    if args.execute:
        ex = next((s for s in (out.get("steps") or []) if isinstance(s, dict) and s.get("step") == "execute_phased"), None)
        if isinstance(ex, dict):
            res = ex.get("result") or {}
            print(f"\nexecute_phased ok={res.get('ok')} stages={res.get('stages_run')}")
            for row in res.get("step_log") or []:
                print(f"  - {row.get('stage')}: motion_ok={row.get('motion_ok')} target={row.get('target_xyz_m')}")

    if out.get("log_path"):
        print(f"\nLog NX: {out.get('log_path')}")

    if not out.get("ok"):
        return 1
    print("\nVERIFY_GRASP_ORBBEC_LOCAL_OK")
    print("Apri nel browser le immagini annotate (bbox verde = visto, arancio = no):")
    for tag in ("front", "wrist_orbbec", "wrist"):
        print(f"  {base}/api/grasp/detection_debug/{tag}.jpg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Percorso didattico dashboard Go2+D1: spiega ogni step e prova solo API sicure (GET).

Uso (PC in LAN con la NX accesa):
  python scripts/stagista_lab_percorso.py http://192.168.123.18:5050
  python scripts/stagista_lab_percorso.py http://192.168.123.18:5052 --out data/stagista_lab_report.json

Scrive anche docs/STAGISTA_PERCORSO_DASHBOARD.md (riepilogo per lo stagista).
Nessun comando Sport, nessun movimento braccio, nessun POST plan/execute.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_OUT = _REPO / "data" / "stagista_lab_report.json"
_DOC_MD = _REPO / "docs" / "STAGISTA_PERCORSO_DASHBOARD.md"


def _get(url: str, timeout: float = 14.0) -> dict:
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = int(resp.status)
            body = resp.read(131072)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            parsed = None
        return {
            "ok": code == 200,
            "http": code,
            "ms": ms,
            "json": parsed,
            "bytes": len(body),
            "error": None,
        }
    except urllib.error.HTTPError as exc:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"ok": False, "http": exc.code, "ms": ms, "json": None, "bytes": 0, "error": f"HTTPError {exc.code}"}
    except urllib.error.URLError as exc:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"ok": False, "http": None, "ms": ms, "json": None, "bytes": 0, "error": f"URLError {exc.reason!r}"}
    except Exception as exc:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"ok": False, "http": None, "ms": ms, "json": None, "bytes": 0, "error": f"{type(exc).__name__}: {exc}"}


def _pick(j: dict | None, *keys: str, max_len: int = 120) -> str:
    if not isinstance(j, dict):
        return "—"
    parts: list[str] = []
    for k in keys:
        if k not in j:
            continue
        v = j[k]
        if v is None or v == "":
            continue
        s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        if len(s) > max_len:
            s = s[: max_len - 1] + "…"
        parts.append(f"{k}={s}")
    return " | ".join(parts) if parts else "-"


LAB_STEPS: list[dict] = [
    {
        "id": "S0",
        "tab": "—",
        "title": "Dashboard viva",
        "explain_it": (
            "Prima di tutto la NX deve rispondere su HTTP. La dashboard è Flask "
            "(``serve_dashboard_lite.py``); ``/api/health`` conferma processo e servizio."
        ),
        "ui": "Qualsiasi tab — badge Edge in alto se ``GO2_LOCAL=1`` sulla Jetson.",
        "safe_get": "/api/health",
        "keys": ("ok", "service", "pid", "process_started_at"),
    },
    {
        "id": "S1",
        "tab": "Scene",
        "title": "Camere log.0 / log.6",
        "explain_it": (
            "Due slot logici: **0** = polso (Orbbec), **6** = frontale (RealSense). "
            "Gli stream MJPEG sono ``/stream/robot/camera/{0|6}.mjpg``. "
            "Lo stato V4L elenca i nodi ``/dev/videoN`` e eventuali warning (es. log.0 assente)."
        ),
        "ui": "Scene → «Aggiorna stato», anteprime MJPEG, picker ◀▶ (solo con tutor).",
        "safe_get": "/api/cameras/status",
        "keys": ("ok", "logical_cameras", "v4l_nodes_detail"),
    },
    {
        "id": "S2",
        "tab": "3D",
        "title": "Scena braccio (FK + mesh)",
        "explain_it": (
            "``GET /api/arm/scene_3d`` restituisce giunti, catena FK, mesh STL opzionali, "
            "feedback servo. Il viewer Three.js in tab 3D fa polling — **solo visualizzazione**."
        ),
        "ui": "3D → Avvia aggiornamento, modalità fast/full, overlay cilindri FK.",
        "safe_get": "/api/arm/scene_3d",
        "keys": ("ok", "servo_feedback_ok", "joints_deg"),
    },
    {
        "id": "S3",
        "tab": "Calib",
        "title": "Tag 5 → base_link",
        "explain_it": (
            "AprilTag id 5 allinea il frame camera al ``base_link`` del braccio. "
            "Senza calibrazione valida, i punti 3D da visione sono meno affidabili. "
            "In lab: prima «Stato», poi «Calibra» **solo con tutor**."
        ),
        "ui": "Calib → anteprima TAG 5; avanzato: nominali X/Y/Z.",
        "safe_get": "/api/arm/calibration_flow",
        "keys": ("ok", "phase", "hint_it"),
    },
    {
        "id": "S4",
        "tab": "Stato",
        "title": "Mission console",
        "explain_it": (
            "Fotografia del deploy: env sicuri (flag arm/base), health worker grasp, "
            "stack NX. È il posto giusto per capire **cosa è abilitato** prima di premere Moto/Presa."
        ),
        "ui": "Stato → Mission console → Aggiorna (auto 8s opzionale).",
        "safe_get": "/api/mission/console",
        "keys": ("ok", "summary", "env"),
    },
    {
        "id": "S5",
        "tab": "Presa",
        "title": "Pipeline grasp (mappa)",
        "explain_it": (
            "Endpoint narrativo: ordine consigliato worker VLA → calib tag → movimento braccio → camere. "
            "Non esegue nulla; descrive ``POST /api/grasp/plan`` e le execute successive."
        ),
        "ui": "Presa — leggere card OpenVLA; **non** «Piano 1 click» / «Muovi IK» senza tutor.",
        "safe_get": "/api/arm/grasp_pipeline",
        "keys": ("ok", "fusion_ready_for_execute", "environment"),
    },
    {
        "id": "S6",
        "tab": "Presa",
        "title": "Health worker cloud",
        "explain_it": (
            "Prima di spendere GPU su EC2/RTX: ``grasp`` in mission console o proxy health. "
            "Verifica URL ``GO2_ANYGRASP_WORKER_URL`` e token configurati."
        ),
        "ui": "Presa → «Health worker» (equivalente a parte di mission console).",
        "safe_get": "/api/grasp/health",
        "keys": ("ok", "worker_url", "reachable"),
    },
    {
        "id": "S7",
        "tab": "Presa",
        "title": "Piano VLA (solo lettura qui)",
        "explain_it": (
            "In UI: «Solo POST plan» invia JPEG log.0+6 al worker → JSON con bbox, heatmap, "
            "``grasp_display_base_link_m`` o ``openvla_action_7dof``. Il piano resta in **cache** "
            "sulla NX per IK/FK execute — **questo script non chiama POST plan**."
        ),
        "ui": "Presa → istruzione IT → «Solo POST plan» → leggere ``graspPlanJson``.",
        "safe_get": None,
        "keys": (),
        "note": "Step manuale in UI o ``verify_go2_lab.py worker`` — non automatizzato (costo/sicurezza).",
    },
    {
        "id": "S8",
        "tab": "Presa",
        "title": "Esecuzione a fasi (concetto)",
        "explain_it": (
            "Dopo un piano validato: ``pre_grasp → approach → grasp → lift`` "
            "(``GO2_GRASP_PHASE_DELAY_MS``). Richiede conferma ``EXECUTE_PHASED_GRASP`` "
            "e ``GO2_ENABLE_REAL_ARM=1``. Stagista: **solo leggere** ``grasp_assessment`` nel JSON."
        ),
        "ui": "Presa → «Sequenza presa (fasi)» — **vietato** senza area libera e tutor.",
        "safe_get": None,
        "keys": (),
        "note": "Vedi ``go2_dashboard/grasp_phased_execute.py``.",
    },
    {
        "id": "S9",
        "tab": "Hermes",
        "title": "Agente linguaggio (preview)",
        "explain_it": (
            "Hermes traduce italiano → intent JSON (Sport, delta giunti, target tool). "
            "Con ``execution_mode: preview`` **non applica** motori: vedi solo proposta + Technical JSON."
        ),
        "ui": "Hermes → radio **preview** → invia testo → approva solo se tutor lo chiede.",
        "safe_get": "/api/hermes/status",
        "keys": ("ok", "enabled", "model"),
    },
    {
        "id": "S10",
        "tab": "Robot",
        "title": "YOLO 2D",
        "explain_it": (
            "Rilevamento oggetti su un frame JPEG: bounding box in pixel, senza muovere il braccio. "
            "Utile per capire cosa «vede» la rete prima del piano 3D."
        ),
        "ui": "Robot → «Rilevamento 2D su log.6».",
        "safe_get": "/api/vision/box_detect?camera=6",
        "keys": ("ok", "boxes"),
    },
]


def _summarize_probe(step: dict, probe: dict | None) -> str:
    if probe is None:
        return str(step.get("note") or "Nessuna probe HTTP (step concettuale).")
    if not probe.get("ok"):
        return f"FAIL {probe.get('error') or probe.get('http')}"
    j = probe.get("json")
    return _pick(j, *step.get("keys", ()))


def run_percorso(base: str) -> dict:
    base = base.rstrip("/")
    steps_out: list[dict] = []
    n_ok = 0
    n_fail = 0
    n_skip = 0

    for step in LAB_STEPS:
        path = step.get("safe_get")
        probe: dict | None = None
        if path:
            url = base + path if path.startswith("/") else f"{base}/{path}"
            probe = _get(url)
            if probe.get("ok"):
                n_ok += 1
            else:
                n_fail += 1
        else:
            n_skip += 1

        steps_out.append(
            {
                "id": step["id"],
                "tab": step["tab"],
                "title": step["title"],
                "explain_it": step["explain_it"],
                "ui": step["ui"],
                "endpoint": path,
                "probe": probe,
                "summary": _summarize_probe(step, probe),
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url": base,
        "steps": steps_out,
        "counts": {"ok": n_ok, "fail": n_fail, "skipped_concept": n_skip},
        "reachable": n_fail == 0 and n_ok > 0,
    }


def _write_markdown(report: dict) -> None:
    lines = [
        "# Percorso stagista — Dashboard Go2 + D1",
        "",
        f"*Generato: {report['generated_at']} UTC · Base: `{report['base_url']}`*",
        "",
        "## Flusso complessivo",
        "",
        "```mermaid",
        "flowchart LR",
        "  S0[Health] --> S1[Camere]",
        "  S1 --> S2[Scene 3D]",
        "  S2 --> S3[Calib tag5]",
        "  S3 --> S4[Mission console]",
        "  S4 --> S5[Pipeline map]",
        "  S5 --> S6[Worker health]",
        "  S6 --> S7[POST plan]",
        "  S7 --> S8[Fasi presa]",
        "  S9[Hermes preview] -.-> S4",
        "  S1 --> S10[YOLO 2D]",
        "```",
        "",
        "## Risultati probe (solo GET)",
        "",
        f"| Esito | OK={report['counts']['ok']} · FAIL={report['counts']['fail']} · step concettuali={report['counts']['skipped_concept']} |",
        "",
        "| Step | Tab | Cosa impari | Probe |",
        "|------|-----|-------------|-------|",
    ]
    for st in report["steps"]:
        ep = st.get("endpoint") or "—"
        summ = (st.get("summary") or "").replace("|", "\\|")
        lines.append(f"| **{st['id']}** {st['title']} | {st['tab']} | {st['explain_it'][:90]}… | `{ep}` → {summ} |")

    lines.extend(
        [
            "",
            "## Dettaglio step (cosa dire allo stagista)",
            "",
        ]
    )
    for st in report["steps"]:
        lines.append(f"### {st['id']} — {st['title']} ({st['tab']})")
        lines.append("")
        lines.append(st["explain_it"])
        lines.append("")
        lines.append(f"- **In UI:** {st['ui']}")
        if st.get("endpoint"):
            lines.append(f"- **API sicura:** `GET {st['endpoint']}`")
        lines.append(f"- **Esito probe:** {st['summary']}")
        lines.append("")

    lines.extend(
        [
            "## Comandi tutor (PC in lab)",
            "",
            "```powershell",
            "python scripts/stagista_lab_percorso.py http://192.168.123.18:5050",
            "python scripts/verify_go2_lab.py dashboard-nx http://192.168.123.18:5050",
            "python scripts/verify_go2_lab.py hermes --http --url http://192.168.123.18:5050",
            "```",
            "",
            "## Zona rossa",
            "",
            "- Tab **Moto**: stick Go2, slider braccio live, Salva ZERO/START",
            "- Tab **Presa**: Piano 1-click, Muovi IK/FK, Sequenza fasi, EC2 start",
            "- Tab **Calib**: «Calibra» / «Cancella file» senza supervisione",
            "",
        ]
    )

    _DOC_MD.parent.mkdir(parents=True, exist_ok=True)
    _DOC_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("base_url", nargs="?", default="http://192.168.123.18:5050")
    ap.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    ap.add_argument("--no-md", action="store_true", help="Non aggiornare docs/STAGISTA_PERCORSO_DASHBOARD.md")
    args = ap.parse_args()

    report = run_percorso(args.base_url)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.no_md:
        _write_markdown(report)

    print(f"STAGISTA_PERCORSO base={report['base_url']}")
    print(f"  reachable={report['reachable']} ok={report['counts']['ok']} fail={report['counts']['fail']}")
    for st in report["steps"]:
        mark = "OK" if (st.get("probe") or {}).get("ok") else ("SKIP" if st.get("probe") is None else "FAIL")
        line = f"  [{mark}] {st['id']} {st['title']}: {st['summary'][:100]}"
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", errors="replace").decode("ascii"))
    print(f"Report JSON: {args.out}")
    if not args.no_md:
        print(f"Guida MD:    {_DOC_MD}")
    return 0 if report["reachable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

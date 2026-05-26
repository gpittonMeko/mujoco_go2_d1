#!/usr/bin/env python3
"""Test integrazione Hermes + camere sulla dashboard reale (tipicamente NX).

Richiede rete verso la Jetson. Non chiama OpenAI se Hermes è spento o senza chiave
(analogamente allo smoke HTTP).

Esempi:
  python scripts/verify_hermes_integration.py --url http://192.168.123.18:5052
  python scripts/verify_hermes_integration.py --url http://127.0.0.1:5052 --probe-depth-previews
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _fail(msg: str) -> None:
    print("FAIL:", msg, file=sys.stderr)


def _ok(msg: str) -> None:
    print("OK:", msg)


def _get_json(url: str, timeout_s: float = 12.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        code = resp.getcode() or 200
        return code, json.loads(raw)


def _head_or_get_bytes(url: str, timeout_s: float = 15.0) -> tuple[int, str, bytes]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            return resp.getcode() or 200, ct, resp.read()
    except urllib.error.HTTPError as exc:
        ct = (exc.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return exc.code, ct, exc.read()


def integration_cameras_and_jpeg(base: str, *, probe_depth: bool) -> bool:
    ok = True
    try:
        code, j = _get_json(f"{base}/api/cameras/status")
    except Exception as exc:
        _fail(f"GET /api/cameras/status: {exc!r}")
        return False
    if code != 200 or not j.get("ok"):
        _fail(f"cameras/status HTTP {code}: {str(j)[:500]}")
        return False
    _ok("GET /api/cameras/status")

    hints = j.get("depth_sysfs_hint_nodes")
    if isinstance(hints, list) and hints:
        _ok(f"depth_sysfs_hint_nodes: {len(hints)} nodi ( sysfs depth/IR )")
        if probe_depth:
            for h in hints[:4]:
                idx = h.get("v4l_index")
                url = h.get("preview_jpg_url") or ""
                if not str(url).strip():
                    continue
                    url = base + url
                elif not (url.startswith("http://") or url.startswith("https://")):
                    url = base + "/" + url.lstrip("/")
                sep = "&" if "?" in url else "?"
                c2, ct, data = _head_or_get_bytes(url + sep + "_probe=1")
                if c2 == 200 and ct == "image/jpeg" and len(data) > 800:
                    _ok(f"depth hint preview v4l={idx} jpeg_bytes={len(data)}")
                else:
                    _fail(f"depth preview v4l={idx} HTTP {c2} ct={ct!r} bytes={len(data)}")
                    ok = False
    elif j.get("go2_local"):
        _ok("depth_sysfs_hint_nodes: lista vuota (nessun sysfs depth/IR in inventario)")
    else:
        _ok("depth_sysfs_hint_nodes: assente (dashboard non GO2_LOCAL — atteso)")

    ou = j.get("openvla_jpeg_urls") or {}
    if isinstance(ou, dict):
        for k in ("logical_0_jpg", "logical_6_jpg"):
            u = ou.get(k)
            if not isinstance(u, str) or not u.strip():
                continue
            if u.startswith("http://") or u.startswith("https://"):
                full = u
            else:
                full = base + (u if u.startswith("/") else "/" + u)
            sep = "&" if "?" in full else "?"
            c2, ct, data = _head_or_get_bytes(full + sep + "_probe=1")
            if c2 == 200 and ct == "image/jpeg" and len(data) > 500:
                _ok(f"JPEG {k}: {len(data)} bytes")
            elif c2 == 503:
                _ok(f"JPEG {k}: 503 (frame unavailable — tipico senza GO2_LOCAL/cam)")
            else:
                _fail(f"JPEG {k}: HTTP {c2} ct={ct!r}")
                ok = False

    for cam in (0, 6):
        u = f"{base}/api/robot/camera/{cam}.jpg?_={cam}"
        c2, ct, data = _head_or_get_bytes(u)
        if c2 == 200 and ct == "image/jpeg" and len(data) > 500:
            _ok(f"GET /api/robot/camera/{cam}.jpg -> {len(data)} bytes")
        elif c2 == 503:
            _ok(f"GET /api/robot/camera/{cam}.jpg -> 503 (no frame / client mode)")
        else:
            _fail(f"camera/{cam}.jpg HTTP {c2} ct={ct!r}")
            ok = False

    return ok


def integration_hermes_dry(base: str) -> bool:
    req = urllib.request.Request(
        f"{base}/api/hermes/command",
        data=json.dumps({"text": "integration dry — rispondi schema JSON minimo", "dry_run": True}).encode(
            "utf-8"
        ),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=95.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = resp.getcode() or 200
    except urllib.error.HTTPError as exc:
        code = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        _fail(f"POST /api/hermes/command: {exc!r}")
        return False

    if code == 503:
        try:
            j = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            j = {}
        reason = j.get("reason", "")
        if reason in {"GO2_ENABLE_HERMES_AGENT_not_enabled", "missing_OPENAI_API_KEY"}:
            _ok(f"POST hermes/command dry -> 503 atteso ({reason})")
            return True
        _fail(f"POST hermes/command unexpected 503: {raw[:400]}")
        return False

    if code != 200:
        _fail(f"POST hermes/command HTTP {code}: {raw[:500]}")
        return False
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        _fail("hermes/command body non JSON")
        return False
    if j.get("dry_run") is not True:
        _fail("dry_run non true nella risposta")
        return False
    _ok("POST /api/hermes/command dry_run -> 200 + intent")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="Base HTTP dashboard operator (es. http://192.168.123.18:5052)")
    ap.add_argument(
        "--probe-depth-previews",
        action="store_true",
        help="Scarica JPEG da depth_sysfs_hint_nodes (solo NX con USB)",
    )
    args = ap.parse_args()
    base = args.url.strip().rstrip("/")

    ok = True
    try:
        code, j = _get_json(f"{base}/api/health")
        if code != 200 or not j.get("ok"):
            _fail(f"/api/health: HTTP {code} {j!r}")
            ok = False
        else:
            _ok("GET /api/health")
    except Exception as exc:
        _fail(f"/api/health: {exc!r}")
        ok = False

    ok = integration_cameras_and_jpeg(base, probe_depth=args.probe_depth_previews) and ok
    ok = integration_hermes_dry(base) and ok

    if ok:
        print("VERIFY_HERMES_INTEGRATION_OK")
        return 0
    print("VERIFY_HERMES_INTEGRATION_FAIL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

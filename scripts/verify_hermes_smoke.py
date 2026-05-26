#!/usr/bin/env python3
"""Smoke test Hermes (offline + opzionale HTTP + opzionale LLM reale).

Esempi:
  python scripts/verify_hermes_smoke.py
  python scripts/verify_hermes_smoke.py --url http://127.0.0.1:5052
  python scripts/verify_hermes_smoke.py --live-openai
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fail(msg: str) -> None:
    print("FAIL:", msg, file=sys.stderr)


def _ok(msg: str) -> None:
    print("OK:", msg)


def smoke_parse_and_payload() -> bool:
    sys.path.insert(0, _repo_root())
    from go2_dashboard.hermes_agent import (  # pylint: disable=import-outside-toplevel
        build_openvla_plan_payload,
        hermes_deployment_context,
        hermes_full_system_prompt,
        parse_llm_json_object,
    )

    raw = '{"assistant_reply_it":"x","base_motion":null,"openvla":null,"arm_preset":null}'
    obj = parse_llm_json_object(raw)
    if obj.get("assistant_reply_it") != "x":
        _fail("parse_llm_json_object content")
        return False
    fenced = '```json\n{"a":1}\n```'
    o2 = parse_llm_json_object(fenced)
    if o2.get("a") != 1:
        _fail("fence strip")
        return False

    p = build_openvla_plan_payload(
        instruction_en="pick up",
        image_url_override=None,
        dashboard_http_origin="http://192.168.123.18:5052",
        script_root="",
        default_logical_cam=6,
    )
    if "/api/robot/camera/6.jpg" not in p.get("image_url", ""):
        _fail(f"payload cam6 url: {p!r}")
        return False

    p0 = build_openvla_plan_payload(
        instruction_en="pick up",
        image_url_override=None,
        dashboard_http_origin="http://192.168.123.18:5052",
        script_root="",
        default_logical_cam=6,
        logical_camera_override=0,
    )
    if "/api/robot/camera/0.jpg" not in p0.get("image_url", ""):
        _fail(f"payload logical_camera_override url: {p0!r}")
        return False

    hp = hermes_full_system_prompt()
    if "Go2" not in hp and "go2" not in hp.lower():
        _fail("system prompt sanity")
        return False

    hp_b = hermes_full_system_prompt(personality="bender_meeting")
    if "vip" not in hp_b.lower():
        _fail("personality_addon_meeting")
        return False

    dc = hermes_deployment_context()
    if "GO2_LOCAL=" not in dc:
        _fail("deployment_context")
        return False

    _ok("parse_llm_json_object + build_openvla_plan_payload + prompts")
    return True


def smoke_flask_import() -> bool:
    sys.path.insert(0, _repo_root())
    os.environ.setdefault("GO2_ENABLE_HERMES_AGENT", "1")
    from go2_dashboard.lite_app import create_operators_app  # pylint: disable=import-outside-toplevel

    app = create_operators_app()
    cli = app.test_client()
    r = cli.get("/api/hermes/status")
    if r.status_code != 200:
        _fail(f"GET /api/hermes/status -> {r.status_code}")
        return False
    data = r.get_json()
    if not isinstance(data, dict) or data.get("ok") is not True:
        _fail(f"status json: {data!r}")
        return False

    cli2 = app.test_client()
    r2 = cli2.post("/api/hermes/command", json={"text": "test", "dry_run": False})
    if r2.status_code not in {503}:
        # Senza chiave nel processo test: 503 missing key; con chiave tenterebbe OpenAI.
        _fail(f"POST hermes/command expected 503 without key, got {r2.status_code}")
        return False
    _ok("Flask test_client GET /api/hermes/status + POST guard (no key -> 503)")
    return True


def smoke_http_base(url: str) -> bool:
    base = url.rstrip("/")
    for path in ("/api/hermes/status", "/api/health"):
        try:
            with urllib.request.urlopen(f"{base}{path}", timeout=8) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                j = json.loads(raw)
                if not j.get("ok"):
                    _fail(f"{path} ok field: {raw[:400]}")
                    return False
        except urllib.error.HTTPError as exc:
            _fail(f"{path} HTTP {exc.code}")
            return False
        except Exception as exc:
            _fail(f"{path}: {exc!r}")
            return False

    dry = urllib.request.Request(
        f"{base}/api/hermes/command",
        data=json.dumps({"text": "solo dry run", "dry_run": True}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(dry, timeout=90) as resp:
            code = resp.getcode() or 200
            raw = resp.read().decode("utf-8", errors="replace")
            j = json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        code = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
        j = {}
        try:
            j = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            pass
        if code == 503 and j.get("reason") in {
            "GO2_ENABLE_HERMES_AGENT_not_enabled",
            "missing_OPENAI_API_KEY",
        }:
            _ok(
                f"POST /api/hermes/command (dry) -> 503 atteso (Hermes o chiave): {j.get('reason')}"
            )
            return True
        _fail(f"POST hermes/command HTTP {code}: {raw[:500]}")
        return False
    except Exception as exc:
        _fail(f"POST hermes/command: {exc!r}")
        return False

    if code != 200:
        _fail(f"POST hermes/command status {code}: {raw[:500]}")
        return False
    if not isinstance(j, dict):
        _fail("POST body not json object")
        return False
    if j.get("dry_run") is not True:
        _fail("expected dry_run true in response")
        return False
    if "intent" not in j:
        _fail("missing intent in dry response")
        return False

    _ok(f"HTTP {base}: health + hermes/status + dry command (LLM hit ok)")
    return True


def smoke_live_openai() -> bool:
    if not openai_api_key():
        _fail("--live-openai richiede OPENAI_API_KEY o GO2_OPENAI_API_KEY nell'ambiente")
        return False
    os.environ["GO2_ENABLE_HERMES_AGENT"] = "1"
    sys.path.insert(0, _repo_root())
    from go2_dashboard.hermes_agent import route_natural_language  # pylint: disable=import-outside-toplevel

    intent = route_natural_language(
        'Return only valid JSON for operator command "stand the robot up" → base_motion stand_up sync true.'
    )
    if not isinstance(intent, dict):
        _fail("route_natural_language not dict")
        return False
    if "assistant_reply_it" not in intent:
        _fail("missing assistant_reply_it")
        return False
    print("intent sample:", json.dumps(intent, indent=2, ensure_ascii=False)[:1200])
    _ok("live OpenAI route_natural_language")
    return True


def openai_api_key() -> str:
    return (os.environ.get("OPENAI_API_KEY") or os.environ.get("GO2_OPENAI_API_KEY") or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="", help="Base HTTP dashboard (es. http://192.168.123.18:5052)")
    ap.add_argument(
        "--live-openai",
        action="store_true",
        help="Una chiamata LLM reale (costa token; richiede chiave in env)",
    )
    args = ap.parse_args()

    ok = True
    ok = smoke_parse_and_payload() and ok
    ok = smoke_flask_import() and ok
    if args.url.strip():
        ok = smoke_http_base(args.url.strip()) and ok
    if args.live_openai:
        ok = smoke_live_openai() and ok

    if ok:
        print("VERIFY_HERMES_SMOKE_OK")
        return 0
    print("VERIFY_HERMES_SMOKE_FAIL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

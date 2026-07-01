#!/usr/bin/env python3
"""Smoke test llama-server locale (Gemma) — OpenAI-compat /v1/chat/completions."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _base_url() -> str:
    raw = (os.environ.get("GO2_HERMES_OPENAI_BASE_URL") or "http://127.0.0.1:8080/v1").strip().rstrip("/")
    return raw or "http://127.0.0.1:8080/v1"


def _model() -> str:
    return (os.environ.get("GO2_HERMES_MODEL") or "gemma-2-2b-it").strip() or "gemma-2-2b-it"


def _get(url: str, timeout: float = 8.0) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _post_chat(prompt: str, timeout: float = 60.0) -> dict:
    url = _base_url() + "/chat/completions"
    body = {
        "model": _model(),
        "messages": [
            {"role": "system", "content": "Rispondi in italiano, una frase breve."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 80,
        "temperature": 0.2,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": "Bearer local-offline",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify local Gemma llama-server")
    ap.add_argument("--base-url", default="", help="Override GO2_HERMES_OPENAI_BASE_URL")
    ap.add_argument("--prompt", default="Ciao, chi sei?")
    args = ap.parse_args()
    if args.base_url.strip():
        os.environ["GO2_HERMES_OPENAI_BASE_URL"] = args.base_url.strip()

    base = _base_url()
    print(f"base={base} model={_model()}")

    code, body = _get(base + "/models")
    print(f"GET /models -> {code}")
    if code != 200:
        print(body[:500])
        return 1

    try:
        out = _post_chat(args.prompt)
    except Exception as exc:
        print("FAIL chat:", repr(exc))
        return 1

    choices = out.get("choices") or []
    msg = ((choices[0] if choices else {}) or {}).get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text:
        print("FAIL empty reply:", json.dumps(out)[:800])
        return 1
    print("OK reply:", text[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Agente Hermes: OpenAI-compatible → Cursor SDK → risposta locale contestuale."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from go2_dashboard.hermes.actions import matches_action_intent, try_action
from go2_dashboard.hermes.context import build_robot_context, context_for_prompt, operator_reachable_quick
from go2_dashboard.hermes.local_agent import local_reply

_SYSTEM = """Sei Hermes, assistente vocale del Go2 con braccio D1.
Rispondi in italiano, 1-3 frasi brevi per sintesi vocale.
Usa solo il JSON contesto; non inventare dati."""


def _openai_reply(user_message: str, history: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
    api_key = (os.environ.get("HERMES_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    base = (os.environ.get("HERMES_OPENAI_BASE") or "https://api.openai.com/v1").strip().rstrip("/")
    model = (os.environ.get("HERMES_OPENAI_MODEL") or "gpt-4o-mini").strip()
    if not api_key:
        raise RuntimeError("HERMES_OPENAI_API_KEY mancante")

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM + "\n\nCONTESTO:\n" + context_for_prompt()},
    ]
    for turn in history[-10:]:
        role = turn.get("role", "user")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": (turn.get("content") or "")[:2000]})
    messages.append({"role": "user", "content": user_message.strip()})

    max_tok = int(os.environ.get("HERMES_LLM_MAX_TOKENS", "120"))
    body = json.dumps({"model": model, "messages": messages, "max_tokens": max_tok}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=float(os.environ.get("HERMES_LLM_TIMEOUT_S", "15"))) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    raw = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    text = str(raw).strip()
    return text or "Nessuna risposta.", {"backend": "openai", "model": model}


def _cursor_sdk_reply(user_message: str, history: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
    from cursor_sdk import Agent, AgentOptions, LocalAgentOptions

    api_key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("CURSOR_API_KEY mancante")

    model = (os.environ.get("HERMES_MODEL") or "composer-2.5").strip()
    repo_root = str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent)

    transcript = ""
    for turn in history[-8:]:
        role = turn.get("role", "user")
        content = (turn.get("content") or "").strip()
        if content:
            transcript += f"\n{role.upper()}: {content}"

    prompt = (
        f"{_SYSTEM}\n\nCONTESTO:\n{context_for_prompt()}\n\n"
        f"CHAT:{transcript}\n\nUTENTE: {user_message.strip()}\n\nHermes:"
    )

    result = Agent.prompt(
        prompt,
        AgentOptions(
            api_key=api_key,
            model=model,
            local=LocalAgentOptions(cwd=repo_root),
        ),
    )
    text = (result.result or "").strip() if result else ""
    return text or "Nessuna risposta.", {"backend": "cursor_sdk", "model": model}


def _fast_mode() -> bool:
    return os.environ.get("HERMES_FAST", "1").lower() in {"1", "true", "yes", "on"}


def _llm_enabled() -> bool:
    if os.environ.get("HERMES_USE_LLM", "").lower() in {"0", "false", "no", "off"}:
        return False
    if _fast_mode() and os.environ.get("HERMES_USE_LLM", "").lower() not in {"1", "true", "yes", "on"}:
        return False
    return bool((os.environ.get("HERMES_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip())


def hermes_reply(user_message: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    history = history or []
    if matches_action_intent(user_message):
        ctx: dict[str, Any] = {"operator_reachable": operator_reachable_quick()}
    else:
        ctx = build_robot_context()
    meta: dict[str, Any] = {}
    reply = ""
    backends = []

    action_out = try_action(user_message, ctx)
    if action_out:
        meta = {k: v for k, v in action_out.items() if k != "reply"}
        return {
            "ok": True,
            "reply": action_out.get("reply", ""),
            "action": action_out.get("action"),
            "sport_ok": action_out.get("sport_ok"),
            "backend": "hermes_sdk",
            "meta": meta,
            "speech_handled": bool(meta.get("speech_handled")),
        }

    if _llm_enabled():
        backends.append("openai")
    if (os.environ.get("CURSOR_API_KEY") or "").strip() and os.environ.get(
        "HERMES_USE_CURSOR_SDK", "0"
    ).lower() in {"1", "true", "yes"}:
        backends.append("cursor_sdk")
    backends.append("local")

    last_err: Exception | None = None
    for name in backends:
        try:
            if name == "openai":
                reply, meta = _openai_reply(user_message, history)
            elif name == "cursor_sdk":
                reply, meta = _cursor_sdk_reply(user_message, history)
            else:
                reply = local_reply(user_message, ctx)
                meta = {"backend": "local"}
            break
        except (urllib.error.URLError, urllib.error.HTTPError, Exception) as exc:
            last_err = exc
            continue

    if not reply:
        reply = local_reply(user_message, ctx)
        meta = {"backend": "local", "fallback_error": repr(last_err) if last_err else None}

    return {"ok": True, "reply": reply, "backend": meta.get("backend", "local"), "meta": meta}

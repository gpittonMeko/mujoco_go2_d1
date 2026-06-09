"""Descrizione scena da JPEG camera (OpenAI vision o fallback breve)."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any


def _openai_vision(jpeg: bytes, prompt: str) -> tuple[str, dict[str, Any]]:
    api_key = (os.environ.get("HERMES_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("HERMES_OPENAI_API_KEY mancante per visione")

    base = (os.environ.get("HERMES_OPENAI_BASE") or "https://api.openai.com/v1").strip().rstrip("/")
    model = (os.environ.get("HERMES_VISION_MODEL") or os.environ.get("HERMES_OPENAI_MODEL") or "gpt-4o-mini").strip()
    detail = (os.environ.get("HERMES_VISION_IMAGE_DETAIL") or "high").strip().lower()
    if detail not in {"low", "high", "auto"}:
        detail = "high"
    b64 = base64.standard_b64encode(jpeg).decode("ascii")

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": detail},
                },
            ],
        }
    ]
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "max_tokens": int(os.environ.get("HERMES_VISION_MAX_TOKENS", "120")),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    timeout_s = float(os.environ.get("HERMES_VISION_TIMEOUT_S", "18"))
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    raw = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    text = str(raw).strip()
    return text or "Non riesco a descrivere l'immagine.", {"backend": "openai_vision", "model": model}


def describe_jpeg(jpeg: bytes, *, camera_label: str = "frontale") -> tuple[str, dict[str, Any]]:
    prompt = (
        f"Camera robot Go2 ({camera_label}), vista davanti al robot. "
        "Descrivi SOLO ciò che è chiaramente visibile nel frame, in italiano, massimo 3 frasi brevi. "
        "Non inventare oggetti o persone. Se non sei sicuro, scrivi «non è chiaro». "
        "Se l'immagine è scura, sfocata o vuota, dillo esplicitamente."
    )
    try:
        return _openai_vision(jpeg, prompt)
    except (urllib.error.URLError, urllib.error.HTTPError, Exception) as exc:
        kb = max(1, len(jpeg) // 1024)
        return (
            f"Ho ricevuto un frame dalla camera {camera_label} ({kb} KB) ma la visione cloud non è disponibile: {exc!s}.",
            {"backend": "vision_fallback", "error": repr(exc)},
        )

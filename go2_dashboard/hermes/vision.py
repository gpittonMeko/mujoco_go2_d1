"""Descrizione scena da JPEG camera (OpenAI vision o fallback locale)."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any


def _local_vision_fallback(jpeg: bytes, *, camera_label: str, error: Exception) -> tuple[str, dict[str, Any]]:
    kb = max(1, len(jpeg) // 1024)
    meta: dict[str, Any] = {"backend": "local_vision_fallback", "cloud_error": repr(error), "bytes": len(jpeg)}
    try:
        import cv2  # type: ignore
        import numpy as np

        arr = np.frombuffer(jpeg, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            raise RuntimeError("jpeg_decode_failed")
        h, w = frame.shape[:2]
        meta["frame_size_px"] = [int(w), int(h)]
        try:
            import sys

            from go2_dashboard.paths import PROJECT_ROOT

            scripts_dir = str(PROJECT_ROOT / "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            from box_object_detector import detect_all_objects, detector_status

            det = detect_all_objects(frame)
            boxes = list(det.get("boxes") or [])
            meta["detector_status"] = detector_status()
            meta["detections"] = boxes[:5]
            if boxes:
                best = boxes[0]
                label = best.get("label") or "oggetto"
                conf = best.get("confidence")
                return (
                    f"Cloud non disponibile. Analisi locale: frame {camera_label} {w}x{h}, rilevo {label}"
                    + (f" confidenza {conf}." if conf is not None else "."),
                    meta,
                )
            return (
                f"Cloud non disponibile. Frame {camera_label} ricevuto ({kb} KB, {w}x{h}); "
                "analisi locale: nessun oggetto affidabile rilevato.",
                meta,
            )
        except Exception as det_exc:
            meta["local_detector_error"] = repr(det_exc)
            return (
                f"Cloud non disponibile. Frame {camera_label} ricevuto ({kb} KB, {w}x{h}); "
                "detector locale non disponibile.",
                meta,
            )
    except Exception as exc:
        meta["decode_error"] = repr(exc)
        return (
            f"Cloud non disponibile. Ho comunque ricevuto un frame dalla camera {camera_label} ({kb} KB).",
            meta,
        )


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
        "Descrivi SOLO cio' che e' chiaramente visibile nel frame, in italiano, massimo 3 frasi brevi. "
        "Non inventare oggetti o persone. Se non sei sicuro, scrivi 'non e' chiaro'. "
        "Se l'immagine e' scura, sfocata o vuota, dillo esplicitamente."
    )
    try:
        return _openai_vision(jpeg, prompt)
    except (urllib.error.URLError, urllib.error.HTTPError, Exception) as exc:
        return _local_vision_fallback(jpeg, camera_label=camera_label, error=exc)

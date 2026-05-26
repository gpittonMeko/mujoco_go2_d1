"""Grasp Coach trial: OpenAI **Chat Completions** vision + JSON (`/v1/chat/completions`),
RGB compressa da camera dashboard + anteprima depth V4L opzionale, avvicinamenti parziali IK sul D1.

Richiede la stessa chiave Hermes: ``OPENAI_API_KEY`` / ``GO2_OPENAI_API_KEY``.
Abilitazione: ``GO2_ENABLE_GRASP_COACH=1``.

**Modelli veloci con vision (2026):** default ``gpt-5-nano``; fallback ``gpt-4.1-nano``, ``gpt-4.1-mini``,
``gpt-4o-mini`` — override ``GO2_GRASP_COACH_MODEL``.

**Target ~1 Hz:** JPEG piccoli, ``detail: low``, ``max_tokens`` limitato, memoria prompt corta,
``GO2_GRASP_COACH_DEPTH_POLICY=alternate`` (depth un passo sì / uno no) per alleggerire upload e latenza."""

from __future__ import annotations

import base64
import json
import math
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

from go2_dashboard.cameras import CAMERA_CACHE, debug_v4l_snapshot_jpeg, v4l_index_in_usb_inventory
from go2_dashboard.grasp_coach_memory import (
    append_grasp_coach_event,
    format_memory_for_prompt,
    read_recent_grasp_coach_events,
)
from go2_dashboard.hermes_agent import (
    hermes_openai_base_url,
    openai_api_key,
    parse_llm_json_object,
)
from go2_dashboard.operator_stack import go2_local
from go2_dashboard.paths import PROJECT_ROOT

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None


def grasp_coach_enabled() -> bool:
    return os.environ.get("GO2_ENABLE_GRASP_COACH", "0").lower() in {"1", "true", "yes", "on"}


def grasp_coach_model() -> str:
    return (os.environ.get("GO2_GRASP_COACH_MODEL") or "gpt-5-nano").strip() or "gpt-5-nano"


def grasp_coach_model_ladder_it() -> list[str]:
    """Suggerimenti ordine velocità/qualità (maggio 2026); override con GO2_GRASP_COACH_MODEL."""
    return [
        "gpt-5-nano (default codice — massima velocità/costo se il tenant lo espone)",
        "gpt-4.1-nano (fallback veloce)",
        "gpt-4.1-mini (più ragionamento)",
        "gpt-4o-mini (compatibilità account vecchi)",
    ]


def _depth_v4l_index_for_logical(logical: int) -> int | None:
    key = f"GO2_DEPTH_VIDEO_INDEX_{int(logical)}"
    raw = os.environ.get(key, os.environ.get("GO2_DEPTH_VIDEO_INDEX", "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _shrink_jpeg_bytes(raw: bytes, *, max_side: int, jpeg_quality: int) -> bytes:
    if cv2 is None or np is None or not raw:
        return raw
    try:
        arr = np.frombuffer(raw, dtype=np.uint8)
        im = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if im is None:
            return raw
        h, w = im.shape[:2]
        m = max(h, w)
        if m <= max_side:
            small = im
        else:
            scale = max_side / float(m)
            small = cv2.resize(im, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
        if not ok or buf is None:
            return raw
        return bytes(buf)
    except Exception:
        return raw


_COACH_SYSTEM = """You are **GraspCoach**, a cautious manipulation advisor for a Unitree Go2 + **D1 arm**.

API transport (informational): responses are obtained via OpenAI **Chat Completions** `POST /v1/chat/completions`
with `response_format: {type: json_object}` and multimodal `image_url` parts (base64 JPEG, `detail: low`).

**RGB vs depth (fusion):**
- **RGB** (first image): edges, texture, object boundary — primary cue for *what* and *where* in the image plane.
- **Depth / IR preview** (second image, when present): relative relief, occlusion, approximate nearer/farther — **not metric** (no mm); use only to disambiguate stacking/overlap and gross distance along view ray.
- When both are present: briefly cross-check RGB segmentation with depth shading; if depth missing this turn, rely on RGB only.
- Optimize for a **~1 Hz** correction loop: short `rationale_en`, decisive JSON.

Rules:
- Rolling memory may include lines marked **OPERATOR_FEEDBACK**: treat them as **hard corrections** for the next moves (safety, direction, speed/grip). If **code_or_prompt_fix_request** appears, you cannot edit the codebase — acknowledge briefly and adapt your JSON advice; suggest concrete wording the operator could put in session note.
- Put human-readable Italian line in `assistant_reply_it`.
- Propose **small partial Cartesian moves** toward a grasp or pre-grasp point in **`base_link`** frame (same as grasp planner IK): x forward from robot, y left, z up. Typical reachable clutter on floor: x≈0.25–0.55 m, |y|<0.22 m, z≈0.00–0.35 m — stay conservative.
- `approach_blend` ∈ (0,1]: fraction of remaining vector **from current tool tip to target** this step should cover. Prefer **0.12–0.28**; never above **0.35** unless operator explicitly demands.
- If unsure, set `target_xyz_base_link_m` null and only advise; explain in `assistant_reply_it`.
- `gripper_command`: **hold** | **open** | **close** — close only when aligned and safe.
- `memory_summary_line`: one English sentence to remember for next turns (what worked / constraint).
- `pose_label_to_save`: optional short label if this pose should be recorded after a successful move (e.g. `pre_grasp_box_A`).

JSON keys (all required):
{
  "assistant_reply_it": "string",
  "memory_summary_line": "string",
  "target_xyz_base_link_m": null | [number, number, number],
  "approach_blend": number,
  "gripper_command": "hold"|"open"|"close",
  "confidence_0_1": number,
  "pose_label_to_save": null | "string",
  "rationale_en": "string"
}
"""


def _openai_coach_call(
    *,
    user_text: str,
    rgb_jpeg: bytes,
    depth_jpeg: bytes | None,
    rgb_label: str,
) -> tuple[dict[str, Any], float]:
    key = openai_api_key()
    if not key:
        raise RuntimeError("missing_OPENAI_API_KEY")

    url = hermes_openai_base_url() + "/chat/completions"
    timeout_s = float((os.environ.get("GO2_GRASP_COACH_TIMEOUT_S") or "22").strip() or "22")

    rgb_b64 = base64.standard_b64encode(rgb_jpeg).decode("ascii")
    parts: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    parts.append({"type": "text", "text": f"── {rgb_label} ──"})
    parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{rgb_b64}", "detail": "low"}})
    if depth_jpeg:
        d_b64 = base64.standard_b64encode(depth_jpeg).decode("ascii")
        parts.append({"type": "text", "text": "── Depth or IR preview (non-metric JPEG) ──"})
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{d_b64}", "detail": "low"}})

    messages = [
        {"role": "system", "content": _COACH_SYSTEM.strip()},
        {"role": "user", "content": parts},
    ]
    try:
        max_tok = int((os.environ.get("GO2_GRASP_COACH_MAX_TOKENS") or "420").strip() or "420")
    except ValueError:
        max_tok = 420
    max_tok = max(180, min(max_tok, 1200))

    payload = {
        "model": grasp_coach_model(),
        "temperature": 0.08,
        "max_tokens": max_tok,
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    ctx = ssl.create_default_context()
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
        raw_r = resp.read().decode("utf-8", errors="replace")
    latency_ms = (time.perf_counter() - t0) * 1000.0
    outer = json.loads(raw_r)
    choices = outer.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("openai_no_choices")
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = (msg or {}).get("content") if isinstance(msg, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("openai_empty_content")
    return parse_llm_json_object(content), latency_ms


def _clamp_blend(raw: Any) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = 0.22
    try:
        mx = float(os.environ.get("GO2_GRASP_COACH_MAX_APPROACH_BLEND", "0.28") or "0.28")
    except ValueError:
        mx = 0.28
    mx = max(0.08, min(mx, 0.42))
    return max(0.06, min(mx, v))


def _sanitize_xyz(raw: Any) -> list[float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        return None
    try:
        x, y, z = float(raw[0]), float(raw[1]), float(raw[2])
    except (TypeError, ValueError):
        return None
    # Loose sandbox — lab tuning via env could tighten later
    if not all(math.isfinite(v) for v in (x, y, z)):
        return None
    if abs(x) > 1.2 or abs(y) > 0.9 or abs(z) > 1.1:
        return None
    return [x, y, z]


def grasp_coach_feedback(body: dict[str, Any]) -> dict[str, Any]:
    """Append feedback operatore sulla memoria rolling (nessuna chiamata OpenAI).

    Usato dopo uno step per correzioni qualitative o richieste di cambio prompt/codice;
    il testo entra nel blocco memory del passo successivo.
    """
    out: dict[str, Any] = {"ok": False, "model": grasp_coach_model()}
    if not grasp_coach_enabled():
        out["reason"] = "GO2_ENABLE_GRASP_COACH_off"
        out["hint_it"] = "Imposta GO2_ENABLE_GRASP_COACH=1 sulla NX e riavvia la dashboard."
        return out
    fb = str(body.get("feedback_text") or body.get("feedback") or "").strip()
    if not fb:
        out["reason"] = "missing_feedback_text"
        return out
    code_note = str(body.get("code_correction_note") or "").strip()
    rsi_raw = body.get("related_step_index")
    rsi: int | None
    try:
        rsi = int(rsi_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        rsi = None
    rrep = str(body.get("related_assistant_reply_it") or "").strip()
    append_grasp_coach_event(
        {
            "kind": "operator_feedback",
            "feedback_text": fb[:2400],
            "code_correction_note": code_note[:2400] if code_note else None,
            "related_step_index": rsi,
            "related_assistant_reply_it": rrep[:600] if rrep else None,
        }
    )
    out["ok"] = True
    out["appended"] = True
    return out


def grasp_coach_step(body: dict[str, Any]) -> dict[str, Any]:
    """Esegue un passo coach (LLM + opzionale motion parziale)."""
    out: dict[str, Any] = {
        "ok": False,
        "openai_api": "chat_completions_json_vision",
        "model": grasp_coach_model(),
    }
    if not grasp_coach_enabled():
        out["reason"] = "GO2_ENABLE_GRASP_COACH_off"
        out["hint_it"] = "Imposta GO2_ENABLE_GRASP_COACH=1 sulla NX e riavvia la dashboard."
        return out
    if not openai_api_key():
        out["reason"] = "missing_OPENAI_API_KEY"
        return out

    instruction = str(body.get("instruction") or body.get("text") or "").strip()
    session_note = str(body.get("session_note") or "").strip()
    if not instruction:
        out["reason"] = "missing_instruction"
        return out

    try:
        logical_rgb = int(body.get("logical_camera_rgb", body.get("logical_camera", 0)))
    except (TypeError, ValueError):
        logical_rgb = 0
    if logical_rgb not in (0, 6):
        logical_rgb = 0

    include_depth_req = bool(body.get("include_depth", True))
    execute = bool(body.get("execute"))

    try:
        step_idx = int(body.get("step_index"))
    except (TypeError, ValueError):
        step_idx = 0

    policy = (os.environ.get("GO2_GRASP_COACH_DEPTH_POLICY") or "alternate").strip().lower()
    if policy not in {"always", "alternate", "rgb_only"}:
        policy = "alternate"

    max_side = int(os.environ.get("GO2_GRASP_COACH_RGB_MAX_SIDE", "300") or "300")
    jpeg_q = int(os.environ.get("GO2_GRASP_COACH_RGB_JPEG_Q", "34") or "34")
    depth_q = int(os.environ.get("GO2_GRASP_COACH_DEPTH_JPEG_Q", "42") or "42")
    depth_max_side = int(os.environ.get("GO2_GRASP_COACH_DEPTH_MAX_SIDE", "256") or "256")

    rgb_raw: bytes | None = None
    depth_raw: bytes | None = None
    depth_small: bytes | None = None
    mem_lines = int(os.environ.get("GO2_GRASP_COACH_MEMORY_LINES", "9") or "9")
    try:
        mem_max_chars = int((os.environ.get("GO2_GRASP_COACH_MEMORY_MAX_CHARS") or "4500").strip() or "4500")
    except ValueError:
        mem_max_chars = 4500
    mem_max_chars = max(1200, min(mem_max_chars, 12000))

    timings_ms: dict[str, float] = {}

    if go2_local():
        CAMERA_CACHE.start()
        try:
            rgb_raw = CAMERA_CACHE.get_jpeg(logical_rgb, wait_s=2.0)
        except Exception:
            rgb_raw = None
        if not rgb_raw:
            rgb_raw = CAMERA_CACHE.peek_jpeg(logical_rgb)

    if not rgb_raw:
        out["reason"] = "no_rgb_frame"
        out["hint_it"] = "Serve GO2_LOCAL=1 e cache camera attiva (tab Presa / Scene)."
        return out

    t_cmp0 = time.perf_counter()
    rgb_small = _shrink_jpeg_bytes(rgb_raw, max_side=max(224, min(max_side, 640)), jpeg_quality=max(26, min(jpeg_q, 85)))
    timings_ms["compress_rgb"] = (time.perf_counter() - t_cmp0) * 1000.0

    attach_depth = False
    if include_depth_req and policy != "rgb_only":
        if policy == "always":
            attach_depth = True
        else:
            attach_depth = step_idx % 2 == 0

    t_dep0 = time.perf_counter()
    if attach_depth and go2_local():
        didx = _depth_v4l_index_for_logical(logical_rgb)
        if didx is not None and v4l_index_in_usb_inventory(didx):
            depth_raw = debug_v4l_snapshot_jpeg(didx, jpeg_quality=max(26, min(depth_q, 90)))
            if depth_raw:
                depth_small = _shrink_jpeg_bytes(
                    depth_raw,
                    max_side=max(160, min(depth_max_side, 420)),
                    jpeg_quality=max(26, min(depth_q, 85)),
                )
    timings_ms["depth_capture_compress_ms"] = (time.perf_counter() - t_dep0) * 1000.0

    recent = read_recent_grasp_coach_events(mem_lines)
    mem_txt = format_memory_for_prompt(recent, max_chars=mem_max_chars)

    rgb_label = (
        "Robot wrist camera (logical 0)"
        if logical_rgb == 0
        else "Robot head camera (logical 6)"
    )

    ctx_block = (
        "--- Rolling memory (recent grasp-coach steps) ---\n"
        + mem_txt
        + "\n--- End memory ---\n"
    )
    if session_note:
        ctx_block += f"\nOperator session note (persistent intent):\n{session_note}\n"

    user_blob = (
        ctx_block
        + "\nOperator instruction (this step):\n"
        + instruction
        + f"\n\nLoop meta: step_index={step_idx}, depth_policy={policy}, depth_attached={bool(depth_small)}.\n"
        "Respond with JSON only — propose cautious partial motion if appropriate."
    )

    try:
        llm, openai_ms = _openai_coach_call(
            user_text=user_blob,
            rgb_jpeg=rgb_small,
            depth_jpeg=depth_small,
            rgb_label=rgb_label,
        )
        timings_ms["openai_http_ms"] = openai_ms
    except Exception as exc:
        out["reason"] = "openai_failed"
        out["detail"] = repr(exc)
        append_grasp_coach_event(
            {
                "operator_instruction": instruction,
                "assistant_reply_it": "",
                "executed": False,
                "motion_ok": None,
                "error": repr(exc),
                "step_index": step_idx,
                "depth_policy": policy,
                "coach_model": grasp_coach_model(),
            }
        )
        return out

    out["coach_json"] = llm
    out["assistant_reply_it"] = str(llm.get("assistant_reply_it") or "").strip()
    out["memory_summary_line"] = str(llm.get("memory_summary_line") or "").strip()
    out["ok"] = True
    out["step_index"] = step_idx
    out["depth_policy"] = policy
    out["depth_attached"] = bool(depth_small)
    out["payload_sizes"] = {
        "rgb_jpeg_bytes": len(rgb_small),
        "depth_jpeg_bytes": len(depth_small) if depth_small else 0,
    }
    out["timings_ms"] = timings_ms
    try:
        timings_ms["pre_openai_total_ms"] = float(timings_ms.get("compress_rgb", 0)) + float(
            timings_ms.get("depth_capture_compress_ms", 0)
        )
    except (TypeError, ValueError):
        timings_ms["pre_openai_total_ms"] = 0.0

    tgt = _sanitize_xyz(llm.get("target_xyz_base_link_m"))
    blend = _clamp_blend(llm.get("approach_blend"))
    grip = str(llm.get("gripper_command") or "hold").strip().lower()
    if grip not in {"hold", "open", "close"}:
        grip = "hold"

    out["interpreted"] = {
        "target_xyz_base_link_m": tgt,
        "approach_blend": blend,
        "gripper_command": grip,
    }

    motion: dict[str, Any] | None = None
    grip_out: dict[str, Any] | None = None

    if execute and go2_local() and tgt is not None:
        from go2_dashboard.d1_arm_publish_lite import (
            current_tool_tip_base_link_m,
            goto_tool_target_base_link_m_partial,
            publish_move_one_joint_deg,
        )
        from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag

        try:
            delay_ms = int((os.environ.get("GO2_GRASP_COACH_DELAY_MS") or "620").strip() or "620")
        except ValueError:
            delay_ms = 620
        delay_ms = max(380, min(delay_ms, 2500))

        if os.environ.get("GO2_GRASP_COACH_BALANCE_HOLD_FIRST", "0").lower() in {"1", "true", "yes", "on"}:
            from go2_dashboard.sport_lane import accompany_execute_json, base_motion_allowed

            ok_b, reason_b = base_motion_allowed()
            if ok_b:
                br, bc = accompany_execute_json(
                    {"mode": "balance_hold", "enable": True, "sync": True},
                    query_sync_flag=True,
                )
                out["base_balance_hold_precursor"] = {
                    "ok": bool(isinstance(br, dict) and br.get("ok") and bc < 400),
                    "http_status": bc,
                    "result": br,
                }
            else:
                out["base_balance_hold_precursor"] = {"ok": False, "skipped": True, "reason": reason_b}

        tip_before, _diag_b = current_tool_tip_base_link_m()
        motion = dict(
            goto_tool_target_base_link_m_partial(tgt, approach_blend=blend, delay_ms=delay_ms)
        )
        motion["tool_tip_base_link_m_before"] = tip_before
        out["motion"] = motion

        angles_after, _diag_a = read_servo_deg_with_diag(PROJECT_ROOT)
        tip_after, _ = current_tool_tip_base_link_m()
        if motion.get("ok") and grip == "close":
            try:
                ang = float((os.environ.get("GO2_GRASP_COACH_GRIPPER_CLOSE_DEG") or "-14").strip())
            except ValueError:
                ang = -14.0
            grip_out = publish_move_one_joint_deg(6, ang)
        elif motion.get("ok") and grip == "open":
            try:
                ang = float((os.environ.get("GO2_GRASP_COACH_GRIPPER_OPEN_DEG") or "22").strip())
            except ValueError:
                ang = 22.0
            grip_out = publish_move_one_joint_deg(6, ang)
        if grip_out is not None:
            out["gripper_motion"] = grip_out

        lbl = llm.get("pose_label_to_save")
        lbl_s = str(lbl).strip() if lbl is not None else ""
        append_grasp_coach_event(
            {
                "operator_instruction": instruction,
                "assistant_reply_it": out["assistant_reply_it"],
                "memory_summary_line": out["memory_summary_line"],
                "executed": True,
                "motion_ok": bool(motion.get("ok")),
                "target_xyz_base_link_m": tgt,
                "approach_blend": blend,
                "gripper_command": grip,
                "tool_tip_base_link_m_before": tip_before,
                "tool_tip_base_link_m_after": tip_after,
                "servo_deg_7_after": angles_after,
                "pose_label": lbl_s or None,
                "rationale_en": str(llm.get("rationale_en") or "")[:400],
                "step_index": step_idx,
                "depth_policy": policy,
                "depth_attached": bool(depth_small),
                "coach_model": grasp_coach_model(),
                "openai_http_ms": timings_ms.get("openai_http_ms"),
            }
        )
    else:
        append_grasp_coach_event(
            {
                "operator_instruction": instruction,
                "assistant_reply_it": out["assistant_reply_it"],
                "memory_summary_line": out["memory_summary_line"],
                "executed": False,
                "motion_ok": None,
                "target_xyz_base_link_m": tgt,
                "approach_blend": blend,
                "gripper_command": grip,
                "rationale_en": str(llm.get("rationale_en") or "")[:400],
                "step_index": step_idx,
                "depth_policy": policy,
                "depth_attached": bool(depth_small),
                "coach_model": grasp_coach_model(),
                "openai_http_ms": timings_ms.get("openai_http_ms"),
            }
        )

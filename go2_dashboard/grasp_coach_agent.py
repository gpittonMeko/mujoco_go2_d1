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
from go2_dashboard.d1_arm_publish_lite import START_VARIANT_LATERAL, normalize_start_variant
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
- Propose partial Cartesian moves toward a grasp or pre-grasp point in **`base_link`** frame (same as grasp planner IK): x forward from robot, y left (+) / right (−), z up. Reachable workspace is **much deeper than it looks**: x≈0.25–0.78 m, |y|≲0.20 m, z≈0.00–0.35 m. A real verified floor-grasp pose was TCP ≈ **[x=0.76, y=+0.08, z=0.01] m** (arm fully stretched forward, slight lateral offset, gripper near floor) — use this as the calibration anchor for "reach far forward, low only when far".
- **ROBOT GEOMETRY (hard physical constraints — TOP PRIORITY):** the **robot's own head + front camera sit directly below/in-front-of the arm base**. Two ways to crash into them:
  1. Going **DOWN (low z) while x is still small** → tool hits the dog's head.
  2. Approaching **straight ahead with y≈0 (arm yaw J0≈0°)** → tool hits the dog's **front camera**.
- **Two-phase + lateral-offset approach (mandatory):**
  - **Phase 1 — REACH OUT:** while the tip is near the body (x ≲ 0.45 m), push x **strongly FORWARD** toward the object and keep z **HIGH (z ≥ ~0.20 m)**. Do **NOT** lower z yet. Each step must be a **clearly visible advance** (cover real distance), not a timid nudge.
  - **Phase 2 — DESCEND:** only once the tip is **well forward / past the head (x ≳ 0.55 m)** may you lower z toward the object (down to ~0.01–0.05 m for a floor pick), as in the anchor pose.
  - **LATERAL OFFSET (always):** never approach perfectly centered. Keep a small lateral offset **|y| ≳ 0.06 m** (object slightly to the LEFT or RIGHT) so the arm is angled and clears the dog's front camera. If the object looks dead-centered, bias y toward whichever side it leans in the image.
- **NO-GO / NO-PICK ZONES — refuse the move (set `target_xyz_base_link_m` = null, `gripper_command` = "hold") and explain in `assistant_reply_it` when:**
  - the only path to the object would require **low z with small x** (over the head), or a **centered y≈0** straight-ahead descent (over the front camera);
  - an **obstacle / another object** sits between the gripper and the target, or the target is partially **occluded / unstable / too close to the robot body**;
  - the object is **outside the reachable workspace** (x>0.78, |y|>0.20, or behind the robot).
  In these cases advise the operator what to change (move the object a bit left/right, clear the obstacle, reposition the dog) instead of forcing a target.
- **Symptom to avoid:** if previous steps kept lowering z and stayed far from the object, you are doing it wrong — switch to a bigger FORWARD x move with z kept high and a small lateral y offset.
- **Framing:** choose the target so the requested object stays **in the camera frame** and gets **closer** every step. The arm must end up **noticeably stretched forward** (large x), not parked near the body.
- `approach_blend` ∈ (0,1]: fraction of remaining vector **from current tool tip to target** this step covers. For Phase-1 forward reach use **0.25–0.35** (decisive advance); use smaller **0.12–0.20** only for fine descent/alignment in Phase 2. Never above **0.40**.
- If unsure, set `target_xyz_base_link_m` null and only advise; explain in `assistant_reply_it`.
- `gripper_command`: **hold** | **open** | **close** — close only when aligned and safe.
- `memory_summary_line`: one English sentence to remember for next turns (what worked / constraint).
- `pose_label_to_save`: optional short label if this pose should be recorded after a successful move (e.g. `pre_grasp_box_A`).

- **OBJECT GROUNDING (required for visual feedback):** report whether the requested object is visible and WHERE in the RGB frame.
  - `object_visible`: true/false. If you cannot clearly see the requested object, set `object_visible=false`, `target_xyz_base_link_m=null`, `gripper_command="hold"` and tell the operator in `assistant_reply_it` what to do (rotate dog, move object, etc.). **Never pretend to grasp an object you cannot see.**
  - `object_pixel_norm`: [u, v] normalized in **0..1** (u=0 left, 1 right; v=0 top, 1 bottom) = center of the requested object in the RGB image; null if not visible. This is overlaid on the operator's screen so they can confirm you found the right object.

JSON keys (all required):
{
  "assistant_reply_it": "string",
  "memory_summary_line": "string",
  "object_visible": true|false,
  "object_pixel_norm": null | [number, number],
  "target_xyz_base_link_m": null | [number, number, number],
  "approach_blend": number,
  "gripper_command": "hold"|"open"|"close",
  "confidence_0_1": number,
  "pose_label_to_save": null | "string",
  "rationale_en": "string"
}
"""

_COACH_SYSTEM_LATERAL = """You are **GraspCoach** for a Unitree Go2 + **D1 arm** in **SIDE / LATERAL START** mode.

The dog has already turned ~90° and the arm is in a **lateral START pose** (J0 ~90°). The **wrist camera (logical 0)**
looks at the object from the side. There is **ample clearance** — the dog's head is **not** in the grasp path.

**LATERAL MODE rules (override frontal constraints):**
- **NO NO-GO zones** for head/camera/frontal cone — approach the object **directly** along the metric depth target.
- **No mandatory lateral y-offset** — centered approaches are OK when the wrist sees the object.
- Prefer targets from **metric Orbbec depth** when provided in context; move the tool tip toward the object in **base_link**
  (x forward, y left+, z up). Reachable: x≈0.15–0.80 m, |y|≲0.35 m, z≈0.00–0.40 m in this pose.
- `approach_blend` ∈ (0,1]: fraction toward target. Use **0.20–0.35** for visible advances; smaller for fine alignment.
- `gripper_command`: **hold** | **open** | **close** — close only when aligned.
- If unsure, set `target_xyz_base_link_m` null and advise in `assistant_reply_it`.

**OBJECT GROUNDING:** `object_visible` true/false; `object_pixel_norm` [u,v] in 0..1 or null.

JSON keys (all required): same schema as frontal mode (`assistant_reply_it`, `memory_summary_line`, `object_visible`,
`object_pixel_norm`, `target_xyz_base_link_m`, `approach_blend`, `gripper_command`, `confidence_0_1`,
`pose_label_to_save`, `rationale_en`).
"""


def _lateral_grasp_mode(body: dict[str, Any] | None = None) -> bool:
    """True in presa laterale: niente NO-GO testa/camera né vincoli di offset angolato frontale."""
    if body and normalize_start_variant(body.get("start_variant")) == START_VARIANT_LATERAL:
        return True
    return os.environ.get("GO2_GRASP_LATERAL_MODE", "0").lower() in {"1", "true", "yes", "on"}


def _nogo_guard_enabled(*, lateral: bool) -> bool:
    if lateral:
        return False
    return os.environ.get("GO2_GRASP_COACH_NOGO_GUARD", "1").lower() in {"1", "true", "yes", "on"}


def _lateral_metric_only_enabled() -> bool:
    """Presa laterale teaching: step coach senza OpenAI — solo Orbbec metrico + IK."""
    return os.environ.get("GO2_GRASP_COACH_LATERAL_METRIC_ONLY", "1").lower() in {"1", "true", "yes", "on"}


def grasp_coach_lateral_metric_only_enabled() -> bool:
    return _lateral_metric_only_enabled()


def grasp_coach_supervisor_enabled() -> bool:
    return _supervisor_enabled()


def _default_coach_blend(body: dict[str, Any]) -> tuple[float, str]:
    blend = _clamp_blend(None)
    blend_source = "metric_default"
    op_blend_raw = body.get("approach_blend_override", body.get("operator_blend"))
    if op_blend_raw is not None:
        try:
            ob = float(op_blend_raw)
        except (TypeError, ValueError):
            ob = 0.0
        if ob > 0:
            try:
                op_mx = float(os.environ.get("GO2_GRASP_COACH_OPERATOR_MAX_BLEND", "0.6") or "0.6")
            except ValueError:
                op_mx = 0.6
            op_mx = max(0.1, min(op_mx, 0.9))
            blend = max(0.04, min(op_mx, ob))
            blend_source = "operator"
    return blend, blend_source


def _metric_grounding_for_step(
    *,
    instruction: str,
    lateral: bool,
    cur_tip: list[float] | None,
) -> dict[str, Any]:
    """Cattura Orbbec + detection + IK metrica (una sola volta per step)."""
    empty: dict[str, Any] = {
        "metric_info": None,
        "metric_coach_stage": None,
        "metric_grasp_final": None,
        "metric_gripper_plan": None,
        "target_source": "model",
        "tgt": None,
        "object_pixel": None,
        "object_visible": False,
    }
    metric_on = os.environ.get("GO2_GRASP_COACH_METRIC_GROUNDING", "1").lower() in {"1", "true", "yes", "on"}
    if not metric_on or not go2_local():
        return empty
    try:
        from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag
        from go2_dashboard.orbbec_wrist_grasp import plan_wrist_grasp_metric

        servo_now, _sdiag = read_servo_deg_with_diag(PROJECT_ROOT)
        if servo_now is None or len(servo_now) < 6:
            return {**empty, "metric_info": {"ok": False, "reason": "no_servo_feedback"}}
        mp = plan_wrist_grasp_metric([float(x) for x in servo_now[:7]], instruction=instruction)
        det = mp.get("object_detection") if isinstance(mp.get("object_detection"), dict) else {}
        stages_dbg: list[dict[str, Any]] = []
        preview = mp.get("preview") if isinstance(mp.get("preview"), dict) else {}
        for st in (preview.get("plan") or []):
            if not isinstance(st, dict):
                continue
            s_tgt = st.get("target_xyz_m")
            s_nogo = (
                None
                if lateral
                else (
                    _nogo_zone_reason([float(v) for v in s_tgt])
                    if isinstance(s_tgt, (list, tuple)) and len(s_tgt) >= 3
                    else None
                )
            )
            stages_dbg.append({
                "stage": st.get("stage"),
                "target_xyz_m": s_tgt,
                "ik_ok": bool(st.get("ik_ok")),
                "collision_dog": s_nogo,
                "safe": bool(st.get("ik_ok") and s_nogo is None),
            })
        metric_info: dict[str, Any] = {
            "ok": bool(mp.get("ok")),
            "reason": mp.get("reason"),
            "grasp_display_base_link_m": mp.get("grasp_display_base_link_m"),
            "label": det.get("label"),
            "confidence": det.get("confidence"),
            "backend": det.get("backend"),
            "bbox_xyxy": det.get("bbox_xyxy"),
            "bbox_center_px": det.get("bbox_center_px"),
            "grip_axis_px": det.get("orient_axis_px") or det.get("grip_axis_px"),
            "orientation_deg": det.get("orientation_deg"),
            "frame_size_px": det.get("frame_size_px"),
            "depth_m": mp.get("depth_m"),
            "reach_m": mp.get("reach_m"),
            "reachable": mp.get("reachable"),
            "ik_stages": stages_dbg,
        }
        snap = mp.get("debug_snapshot") if isinstance(mp.get("debug_snapshot"), dict) else {}
        if snap.get("image_url"):
            metric_info["viz_image_url"] = str(snap["image_url"])
        result = {
            **empty,
            "metric_info": metric_info,
            "target_source": "model",
            "tgt": None,
            "object_pixel": None,
            "object_visible": False,
            "metric_coach_stage": None,
            "metric_grasp_final": None,
            "metric_gripper_plan": None,
        }
        if mp.get("ok"):
            mtgt, mstage = _coach_target_from_metric_plan(mp, cur_tip, lateral=lateral)
            if mtgt is not None:
                result["tgt"] = mtgt
                result["metric_coach_stage"] = mstage
                result["metric_grasp_final"] = mp.get("grasp_display_base_link_m")
                result["metric_gripper_plan"] = (
                    mp.get("preview", {}).get("gripper") if isinstance(mp.get("preview"), dict) else None
                )
                metric_info["coach_target_base_link_m"] = mtgt
                metric_info["coach_stage"] = mstage
                metric_info["grasp_final_base_link_m"] = result["metric_grasp_final"]
                result["target_source"] = "metric_orbbec"
                result["object_visible"] = True
                ctr = det.get("bbox_center_px")
                fsz = det.get("frame_size_px")
                if (
                    isinstance(ctr, (list, tuple))
                    and len(ctr) >= 2
                    and isinstance(fsz, (list, tuple))
                    and len(fsz) >= 2
                    and float(fsz[0]) > 0
                    and float(fsz[1]) > 0
                ):
                    result["object_pixel"] = [
                        max(0.0, min(1.0, float(ctr[0]) / float(fsz[0]))),
                        max(0.0, min(1.0, float(ctr[1]) / float(fsz[1]))),
                    ]
        return result
    except Exception as exc:
        return {**empty, "metric_info": {"ok": False, "reason": "metric_grounding_error", "detail": repr(exc)}}


def _coach_system_prompt(*, lateral: bool) -> str:
    return _COACH_SYSTEM_LATERAL.strip() if lateral else _COACH_SYSTEM.strip()


def _openai_coach_call(
    *,
    user_text: str,
    rgb_jpeg: bytes,
    depth_jpeg: bytes | None,
    rgb_label: str,
    system_prompt: str | None = None,
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
        {"role": "system", "content": (system_prompt or _COACH_SYSTEM).strip()},
        {"role": "user", "content": parts},
    ]
    try:
        max_tok = int((os.environ.get("GO2_GRASP_COACH_MAX_TOKENS") or "420").strip() or "420")
    except ValueError:
        max_tok = 420
    max_tok = max(180, min(max_tok, 1200))

    model = grasp_coach_model()
    ml = model.lower()
    # gpt-5* e o-series (o1/o3/o4) usano max_completion_tokens e accettano solo temperature di default (=1):
    # mandare temperature!=1 o max_tokens fa tornare 400 Bad Request.
    new_style = (
        ml.startswith("gpt-5")
        or ml.startswith("o1")
        or ml.startswith("o3")
        or ml.startswith("o4")
    )
    payload: dict[str, Any] = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    if new_style:
        # i reasoning model consumano token di reasoning: lascia margine per il contenuto JSON.
        payload["max_completion_tokens"] = max(max_tok, 768)
        if ml.startswith("gpt-5"):
            # mantiene il loop ~1 Hz ed evita risposte vuote (tutto budget speso in reasoning).
            payload["reasoning_effort"] = (
                os.environ.get("GO2_GRASP_COACH_REASONING_EFFORT") or "minimal"
            ).strip() or "minimal"
    else:
        payload["temperature"] = 0.08
        payload["max_tokens"] = max_tok
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
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
            raw_r = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as he:
        try:
            body = he.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        # estrai il messaggio OpenAI (error.message) se presente, altrimenti corpo grezzo.
        msg_txt = body
        try:
            j = json.loads(body)
            err = j.get("error") if isinstance(j, dict) else None
            if isinstance(err, dict) and err.get("message"):
                msg_txt = str(err.get("message"))
        except Exception:
            pass
        raise RuntimeError(f"openai_http_{he.code}: {msg_txt[:500]}") from he
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


def _clip_xyz_workspace(xyz: list[float]) -> list[float]:
    """Riduce un target 3D fuori sandbox al workspace parziale (approach coach)."""
    x, y, z = float(xyz[0]), float(xyz[1]), float(xyz[2])
    return [
        max(0.10, min(1.15, x)),
        max(-0.88, min(0.88, y)),
        max(0.05, min(1.05, z)),
    ]


def _nogo_zone_reason(xyz: list[float]) -> str | None:
    """NO-GO / NO-PICK guard (sicurezza dura): rifiuta target che colpirebbero testa/camera del cane.

    Per scendere in basso (z piccolo) il braccio DEVE essere ben steso in avanti (x grande) e con
    un offset laterale (|y| non centrato), altrimenti la punta sbatte sulla testa / camera frontale
    del Go2. Soglie regolabili via env. Vedi posa di presa reale TCP≈[0.76, 0.08, 0.01].
    """
    x, y, z = xyz[0], xyz[1], xyz[2]

    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, str(default)) or default)
        except (TypeError, ValueError):
            return default

    z_min = _f("GO2_GRASP_COACH_NOGO_Z_MIN", 0.18)
    x_min = _f("GO2_GRASP_COACH_NOGO_X_MIN", 0.55)
    cone_deg = _f("GO2_GRASP_COACH_NOGO_CONE_DEG", 10.0)
    x_max = _f("GO2_GRASP_COACH_REACH_X_MAX", 0.80)
    y_max = _f("GO2_GRASP_COACH_REACH_Y_MAX", 0.22)

    if x > x_max or abs(y) > y_max or x < 0.10:
        return f"target fuori workspace (x={x:.2f}, y={y:.2f}); reach x≤{x_max:.2f}, |y|≤{y_max:.2f}"
    if z < z_min and x < x_min:
        return (
            f"NO-GO testa: z basso ({z:.2f}<{z_min:.2f}) con x piccolo ({x:.2f}<{x_min:.2f}) → "
            "il braccio sbatterebbe sulla testa del cane; estendi in avanti (x maggiore) prima di scendere"
        )
    # Cono centrale: scendere (z basso) entro ±cone_deg dall'asse frontale colpisce la testa/camera.
    yaw_deg = abs(math.degrees(math.atan2(y, max(x, 1e-6))))
    if z < z_min and yaw_deg < cone_deg:
        return (
            f"NO-GO cono centrale: discesa entro ±{cone_deg:.0f}° dall'asse frontale (yaw={yaw_deg:.1f}°) con z basso → "
            "sposta il target un po' a destra o a sinistra (fuori dal cono centrale) per evitare testa/camera del cane"
        )
    return None


def _coach_target_from_metric_plan(
    mp: dict[str, Any],
    cur_tip: list[float] | None,
    *,
    lateral: bool = False,
) -> tuple[list[float] | None, str | None]:
    """Waypoint per il coach: stadio IK sicuro (pre_grasp/approach), non la presa finale a z basso.

    Ritorna ``(target_xyz_base_link_m, stage_name)``: lo stadio selezionato serve all'auto-close
    (chiudere solo quando il target scelto e' lo stadio ``grasp``).
    """
    preview = mp.get("preview") if isinstance(mp.get("preview"), dict) else {}
    plan = preview.get("plan") if isinstance(preview.get("plan"), list) else []
    max_reach = float(os.environ.get("GO2_ARM_MAX_REACH_M", "0.55") or 0.55)
    max_coach_m = float(os.environ.get("GO2_GRASP_MAX_COACH_TARGET_M", "0.55") or 0.55)
    # Avanzamento di fase: se il TCP e' gia vicino al target di uno stadio intermedio
    # (pre_grasp/approach) lo si salta e si punta lo stadio piu' profondo, cosi sopra
    # l'oggetto il braccio SCENDE fino a `grasp` (e l'auto-close della pinza scatta).
    advance_m = float(os.environ.get("GO2_GRASP_STAGE_ADVANCE_M", "0.05") or 0.05)
    reachable_plan = mp.get("reachable") is not False
    for prefer in ("pre_grasp", "approach", "grasp", "lift"):
        for st in plan:
            if not isinstance(st, dict) or st.get("stage") != prefer or not st.get("ik_ok"):
                continue
            s_tgt = st.get("target_xyz_m")
            if not isinstance(s_tgt, (list, tuple)) or len(s_tgt) < 3:
                continue
            xyz = [float(s_tgt[i]) for i in range(3)]
            if not lateral and _nogo_zone_reason(xyz) is not None:
                continue
            if not lateral and cur_tip is not None and xyz[0] < cur_tip[0] - 0.02:
                continue
            if cur_tip is not None and len(cur_tip) >= 3:
                d_tip = math.sqrt(sum((float(cur_tip[i]) - xyz[i]) ** 2 for i in range(3)))
                if d_tip > max_coach_m:
                    continue
                # Gia su questo stadio intermedio -> avanza allo stadio successivo (scendi).
                if prefer in ("pre_grasp", "approach") and d_tip <= advance_m:
                    continue
            if not reachable_plan and prefer in ("grasp", "lift"):
                continue
            return _sanitize_xyz(xyz), prefer
    raw = mp.get("grasp_display_base_link_m")
    san = _sanitize_xyz(raw)
    if san is not None and reachable_plan:
        if cur_tip is not None and len(cur_tip) >= 3:
            d_tip = math.sqrt(sum((float(cur_tip[i]) - san[i]) ** 2 for i in range(3)))
            if d_tip <= max_coach_m:
                return san, "grasp"
        elif san[2] >= 0.05:
            return san, "grasp"
    return None, None


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


_SUPERVISOR_SYSTEM = """You are GraspSupervisor — second-check ONLY for a metric Orbbec grasp plan.
You NEVER output target XYZ or joint angles. You only approve or veto execution based on the RGB image.

The operator requested a colored box. Check:
- object_matches_instruction: does the highlighted/detected box match the requested color?
- object_visible: is a graspable box clearly visible?
- approve: true only if safe to execute partial approach this step
- suggested_blend: 0.12-0.35 or null to keep default
- reason_it: short Italian explanation

JSON keys (all required):
{
  "approve": true|false,
  "object_matches_instruction": true|false,
  "object_visible": true|false,
  "suggested_blend": null | number,
  "reason_it": "string"
}"""


def _supervisor_enabled() -> bool:
    return os.environ.get("GO2_GRASP_COACH_SUPERVISOR", "1").lower() in {"1", "true", "yes", "on"}


def grasp_supervisor_review(
    *,
    instruction: str,
    metric_plan: dict[str, Any],
    rgb_jpeg: bytes | None = None,
    color_hint: str | None = None,
) -> dict[str, Any]:
    """GPT-nano second check before autonomous/metric execute. Does not replace IK target."""
    out: dict[str, Any] = {"ok": False, "skipped": False, "approve": True}
    if not _supervisor_enabled():
        out.update({"ok": True, "skipped": True, "approve": True, "reason_it": "supervisor_off"})
        return out
    if not grasp_coach_enabled() or not openai_api_key():
        out.update({"ok": True, "skipped": True, "approve": True, "reason_it": "no_openai_key"})
        return out
    det = metric_plan.get("object_detection") if isinstance(metric_plan.get("object_detection"), dict) else {}
    summary = {
        "instruction": instruction,
        "color_hint": color_hint or det.get("color_hint"),
        "detection_label": det.get("label"),
        "confidence": det.get("confidence"),
        "depth_m": metric_plan.get("depth_m"),
        "grasp_xyz": metric_plan.get("grasp_display_base_link_m"),
        "reachable": metric_plan.get("reachable"),
        "teach_calib_applied": metric_plan.get("teach_calib_applied"),
        "online_calib_applied": metric_plan.get("online_calib_applied"),
    }
    user_blob = (
        "Metric plan summary (IK target is fixed server-side — do NOT propose coordinates):\n"
        + json.dumps(summary, ensure_ascii=False)
        + "\nApprove partial arm motion toward this object?"
    )
    img = rgb_jpeg
    if not img and go2_local():
        try:
            CAMERA_CACHE.start()
            img = CAMERA_CACHE.get_jpeg(0, wait_s=1.5) or CAMERA_CACHE.peek_jpeg(0)
        except Exception:
            img = None
    if not img:
        out.update({"ok": True, "skipped": True, "approve": True, "reason_it": "no_rgb_frame"})
        return out
    small = _shrink_jpeg_bytes(img, max_side=320, jpeg_quality=34)
    try:
        llm, ms = _openai_coach_call(
            user_text=user_blob,
            rgb_jpeg=small,
            system_prompt=_SUPERVISOR_SYSTEM,
            depth_jpeg=None,
            rgb_label="Robot wrist camera (logical 0)",
        )
        out["openai_ms"] = ms
        out["supervisor_json"] = llm
        approve = bool(llm.get("approve", False))
        obj_match = bool(llm.get("object_matches_instruction", approve))
        visible = bool(llm.get("object_visible", approve))
        out.update(
            {
                "ok": True,
                "approve": approve and obj_match and visible,
                "object_matches_instruction": obj_match,
                "object_visible": visible,
                "reason_it": str(llm.get("reason_it") or ""),
                "suggested_blend": llm.get("suggested_blend"),
            }
        )
        return out
    except Exception as exc:
        out["reason"] = "supervisor_openai_failed"
        out["detail"] = repr(exc)
        out["approve"] = True
        out["skipped"] = True
        out["reason_it"] = "Supervisor fallito — procedo con metrica (fallback)."
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

    instruction = str(body.get("instruction") or body.get("text") or "").strip()
    session_note = str(body.get("session_note") or "").strip()
    if not instruction:
        out["reason"] = "missing_instruction"
        return out

    lateral = _lateral_grasp_mode(body)
    skip_openai = bool(lateral and _lateral_metric_only_enabled())
    out["start_variant"] = normalize_start_variant(body.get("start_variant"))
    out["lateral_grasp_mode"] = lateral
    out["coach_mode"] = "lateral_metric_only" if skip_openai else "llm_vision"
    if not skip_openai and not openai_api_key():
        out["reason"] = "missing_OPENAI_API_KEY"
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
    rgb_small: bytes | None = None

    # Posizione CORRENTE della punta utensile (FK) in base_link.
    cur_tip: list[float] | None = None
    if go2_local():
        try:
            from go2_dashboard.d1_arm_publish_lite import current_tool_tip_base_link_m

            cur_tip, _tip_diag = current_tool_tip_base_link_m()
        except Exception:
            cur_tip = None
    out["tool_tip_base_link_m_now"] = cur_tip

    metric_result = _metric_grounding_for_step(
        instruction=instruction,
        lateral=lateral,
        cur_tip=cur_tip,
    )
    metric_info: dict[str, Any] | None = metric_result.get("metric_info")
    metric_coach_stage: str | None = metric_result.get("metric_coach_stage")
    metric_grasp_final: Any = metric_result.get("metric_grasp_final")
    metric_gripper_plan: Any = metric_result.get("metric_gripper_plan")
    target_source: str = str(metric_result.get("target_source") or "model")
    tgt: list[float] | None = metric_result.get("tgt")
    object_visible = bool(metric_result.get("object_visible"))
    object_pixel: list[float] | None = metric_result.get("object_pixel")
    blend_source = "metric_default"
    blend, blend_source = _default_coach_blend(body)
    grip = "hold"
    llm: dict[str, Any] = {}

    if skip_openai:
        out["metric_grounding"] = metric_info
        if metric_info and metric_info.get("viz_image_url"):
            out["metric_viz_url"] = str(metric_info["viz_image_url"])
        if not (metric_info and metric_info.get("ok") and tgt is not None):
            out["reason"] = (metric_info or {}).get("reason") or "no_detection"
            out["object_visible"] = False
            out["hint_it"] = (
                "Orbbec non ha visto la scatola nel frame polso — premi «Acquisizione e stima» "
                "o ricalibra il colore; lo step laterale non usa OpenAI."
            )
            return out
        stage = metric_coach_stage or "pre_grasp"
        out["ok"] = True
        out["step_index"] = step_idx
        out["object_visible"] = True
        out["object_pixel_norm"] = object_pixel
        out["assistant_reply_it"] = (
            f"Step metrico laterale: avvicino verso {stage} "
            f"(target x={tgt[0]:.2f} y={tgt[1]:.2f} z={tgt[2]:.2f}) — depth Orbbec, senza LLM."
        )
        out["coach_json"] = {
            "source": "lateral_metric_only",
            "coach_stage": stage,
            "target_xyz_base_link_m": tgt,
            "approach_blend": blend,
            "gripper_command": grip,
        }
        out["depth_policy"] = "metric_orbbec"
        out["depth_attached"] = True
        out["timings_ms"] = timings_ms
    else:
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
        rgb_small = _shrink_jpeg_bytes(
            rgb_raw, max_side=max(224, min(max_side, 640)), jpeg_quality=max(26, min(jpeg_q, 85))
        )
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

        if cur_tip is not None:
            if lateral:
                state_block = (
                    f"\n--- ARM STATE NOW (base_link, meters) — LATERAL START ---\n"
                    f"current_tool_tip = [x={cur_tip[0]:.3f}, y={cur_tip[1]:.3f}, z={cur_tip[2]:.3f}]\n"
                    "WRIST camera (logical 0). Side approach: move directly toward the metric target / object in frame. "
                    "NO head/camera NO-GO zones in this mode.\n"
                    "--- End arm state ---\n"
                )
            else:
                state_block = (
                    f"\n--- ARM STATE NOW (base_link, meters) ---\n"
                    f"current_tool_tip = [x={cur_tip[0]:.3f}, y={cur_tip[1]:.3f}, z={cur_tip[2]:.3f}]\n"
                    "The RGB image is from the WRIST camera mounted near the gripper, so it shows roughly what the tool tip is pointing at.\n"
                    "To REACH the object you must move the tip TOWARD it. If the object is not yet grasped, your target x is almost always "
                    "**>= current x** (extend further forward) — do NOT propose an x smaller than current x (that retracts the arm away from the object). "
                    "Move toward where the object appears in the frame: object on the right of the frame → decrease y; on the left → increase y; "
                    "lower in the frame / closer → only lower z once x is already large (past the head).\n"
                    "--- End arm state ---\n"
                )
        else:
            state_block = (
                "\n--- ARM STATE NOW ---\ncurrent_tool_tip unavailable this step; be conservative.\n--- End arm state ---\n"
            )

        user_blob = (
            ctx_block
            + state_block
            + "\nOperator instruction (this step):\n"
            + instruction
            + f"\n\nLoop meta: step_index={step_idx}, depth_policy={policy}, depth_attached={bool(depth_small)}, lateral_mode={lateral}.\n"
            "Respond with JSON only — propose cautious partial motion if appropriate."
        )

        try:
            llm, openai_ms = _openai_coach_call(
                user_text=user_blob,
                rgb_jpeg=rgb_small,
                system_prompt=_coach_system_prompt(lateral=lateral),
                depth_jpeg=depth_small,
                rgb_label=rgb_label,
            )
            timings_ms["openai_http_ms"] = openai_ms
        except Exception as exc:
            out["reason"] = "openai_failed"
            out["detail"] = repr(exc)
            out["metric_grounding"] = metric_info
            if metric_info and metric_info.get("viz_image_url"):
                out["metric_viz_url"] = str(metric_info["viz_image_url"])
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
        object_visible = bool(llm.get("object_visible", True))
        object_pixel = None
        _opx = llm.get("object_pixel_norm")
        if isinstance(_opx, (list, tuple)) and len(_opx) >= 2:
            try:
                u, v = float(_opx[0]), float(_opx[1])
                if math.isfinite(u) and math.isfinite(v):
                    object_pixel = [max(0.0, min(1.0, u)), max(0.0, min(1.0, v))]
            except (TypeError, ValueError):
                object_pixel = None
        out["object_visible"] = object_visible
        out["object_pixel_norm"] = object_pixel
        out["ok"] = True
        out["step_index"] = step_idx
        out["depth_policy"] = policy
        out["depth_attached"] = bool(depth_small)
        out["payload_sizes"] = {
            "rgb_jpeg_bytes": len(rgb_small),
            "depth_jpeg_bytes": len(depth_small) if depth_small else 0,
        }
        if os.environ.get("GO2_GRASP_COACH_RETURN_PREVIEW", "1").lower() in {"1", "true", "yes", "on"}:
            try:
                out["rgb_preview_b64"] = base64.b64encode(rgb_small).decode("ascii")
            except Exception:
                out["rgb_preview_b64"] = None
        out["timings_ms"] = timings_ms
        try:
            timings_ms["pre_openai_total_ms"] = float(timings_ms.get("compress_rgb", 0)) + float(
                timings_ms.get("depth_capture_compress_ms", 0)
            )
        except (TypeError, ValueError):
            timings_ms["pre_openai_total_ms"] = 0.0

        tgt = _sanitize_xyz(llm.get("target_xyz_base_link_m"))
        blend = _clamp_blend(llm.get("approach_blend"))
        blend_source = "model"
        op_blend_raw = body.get("approach_blend_override", body.get("operator_blend"))
        if op_blend_raw is not None:
            try:
                ob = float(op_blend_raw)
            except (TypeError, ValueError):
                ob = 0.0
            if ob > 0:
                try:
                    op_mx = float(os.environ.get("GO2_GRASP_COACH_OPERATOR_MAX_BLEND", "0.6") or "0.6")
                except ValueError:
                    op_mx = 0.6
                op_mx = max(0.1, min(op_mx, 0.9))
                blend = max(0.04, min(op_mx, ob))
                blend_source = "operator"
        grip = str(llm.get("gripper_command") or "hold").strip().lower()
        if grip not in {"hold", "open", "close"}:
            grip = "hold"

        # Metrica già calcolata sopra: se Orbbec vede l'oggetto, sovrascrive il target LLM.
        if metric_info and metric_info.get("ok") and metric_result.get("tgt") is not None:
            tgt = metric_result["tgt"]
            metric_coach_stage = metric_result.get("metric_coach_stage")
            metric_grasp_final = metric_result.get("metric_grasp_final")
            metric_gripper_plan = metric_result.get("metric_gripper_plan")
            target_source = "metric_orbbec"
            object_visible = True
            object_pixel = metric_result.get("object_pixel")
            out["object_visible"] = True
            out["object_pixel_norm"] = object_pixel

    out["target_source"] = target_source
    out["metric_grounding"] = metric_info
    if metric_info and metric_info.get("viz_image_url"):
        out["metric_viz_url"] = metric_info["viz_image_url"]

    # NO-GO / NO-PICK guard lato server (disattivato in modalità laterale).
    nogo_reason: str | None = None
    guard_on = _nogo_guard_enabled(lateral=lateral)
    metric_ok = bool(metric_info and metric_info.get("ok"))
    if guard_on and not object_visible and not metric_ok:
        nogo_reason = "oggetto non visibile nel frame: nessun movimento (ruota il cane o riposiziona l'oggetto)"
        tgt = None
        grip = "hold"
    # Target da depth metrica: NO-GO già valutato sugli stadi IK; non bloccare la presa finale a z basso.
    if tgt is not None and guard_on and target_source != "metric_orbbec":
        nogo_reason = _nogo_zone_reason(tgt)
        # Anti-ritrazione: durante l'avvicinamento (non chiusura) non lasciare che il braccio
        # torni indietro (target x molto minore della punta attuale) — è il bug che faceva
        # ritrarre il braccio già steso a x≈0.71 verso target ciechi x≈0.4. NON si applica al
        # target metrico (depth reale): se l'oggetto è davvero più vicino, avvicinarsi è corretto.
        if nogo_reason is None and cur_tip is not None and grip != "close" and target_source != "metric_orbbec":
            try:
                back_margin = float(os.environ.get("GO2_GRASP_COACH_NO_RETRACT_MARGIN_M", "0.04") or "0.04")
            except ValueError:
                back_margin = 0.04
            if tgt[0] < cur_tip[0] - back_margin:
                nogo_reason = (
                    f"anti-ritrazione: target x={tgt[0]:.2f} < punta attuale x={cur_tip[0]:.2f} → "
                    "il braccio si ritrarrebbe invece di allungarsi verso l'oggetto; estendi in avanti"
                )
        if nogo_reason is not None:
            grip = "hold"

    out["interpreted"] = {
        "target_xyz_base_link_m": tgt,
        "approach_blend": blend,
        "approach_blend_source": blend_source,
        "gripper_command": grip,
        "nogo_zone": nogo_reason,
        "nogo_blocked": bool(nogo_reason is not None),
        "object_visible": object_visible,
        "object_pixel_norm": object_pixel,
        "tool_tip_base_link_m_now": cur_tip,
        "target_source": target_source,
    }
    if nogo_reason is not None:
        out["assistant_reply_it"] = (
            "[NO-GO] " + str(out.get("assistant_reply_it") or "") + f" — Mossa bloccata: {nogo_reason}."
        )

    motion: dict[str, Any] | None = None
    grip_out: dict[str, Any] | None = None

    if execute and go2_local() and tgt is not None and nogo_reason is None:
        from go2_dashboard import d1_arm_motion
        from go2_dashboard.d1_arm_publish_lite import (
            current_tool_tip_base_link_m,
            goto_tool_target_base_link_m_partial,
            publish_move_one_joint_deg,
        )
        from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag

        # Stesso stack del tab «Braccio D1 · giunti»: sessione live + daemon DDS persistente (no cedimento).
        sess = d1_arm_motion.ensure_grasp_motion_worker()
        out["arm_motion_worker"] = sess
        if not (sess.get("ok") or sess.get("skipped")):
            out["motion"] = {"ok": False, "reason": "arm_session_failed", "motion_worker": sess}
            return out

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
        # Verifica cinematica: il TCP è davvero arrivato al waypoint comandato? (punto 1 operatore)
        wp = motion.get("waypoint_base_link_m")
        if isinstance(wp, (list, tuple)) and len(wp) >= 3 and isinstance(tip_after, (list, tuple)) and len(tip_after) >= 3:
            try:
                err_vec = [float(tip_after[i]) - float(wp[i]) for i in range(3)]
                err_norm = math.sqrt(sum(e * e for e in err_vec))
                motion["tcp_reach_error_m"] = round(err_norm, 4)
                motion["tcp_reach_error_vec_m"] = [round(e, 4) for e in err_vec]
                motion["tcp_reach_ok"] = bool(err_norm <= float(os.environ.get("GO2_GRASP_COACH_TCP_TOL_M", "0.03") or "0.03"))
            except (TypeError, ValueError):
                pass

        # AUTO-CLOSE DAL PIANO METRICO: chiudere quando il TCP ha raggiunto lo stadio ``grasp``
        # del piano Orbbec, indipendentemente dal suggerimento dell'LLM (solo modalità laterale).
        auto_close = False
        auto_close_hold_s = 0.6
        if (
            os.environ.get("GO2_GRASP_COACH_AUTOCLOSE_FROM_PLAN", "1").lower() in {"1", "true", "yes", "on"}
            and target_source == "metric_orbbec"
            and lateral
            and motion.get("ok")
            and motion.get("tcp_reach_ok") is not False
        ):
            try:
                close_dist = float(os.environ.get("GO2_GRASP_COACH_AUTOCLOSE_DIST_M", "0.04") or "0.04")
            except ValueError:
                close_dist = 0.04
            near_grasp = False
            if (
                isinstance(metric_grasp_final, (list, tuple))
                and len(metric_grasp_final) >= 3
                and isinstance(tip_after, (list, tuple))
                and len(tip_after) >= 3
            ):
                try:
                    d = math.sqrt(
                        sum((float(tip_after[i]) - float(metric_grasp_final[i])) ** 2 for i in range(3))
                    )
                    motion["dist_to_grasp_final_m"] = round(d, 4)
                    near_grasp = d <= close_dist
                except (TypeError, ValueError):
                    pass
            if metric_coach_stage == "grasp" or near_grasp:
                auto_close = True
                if isinstance(metric_gripper_plan, list):
                    for g in metric_gripper_plan:
                        if isinstance(g, dict) and g.get("stage") == "grasp":
                            try:
                                auto_close_hold_s = float(g.get("hold_s") or 0.6)
                            except (TypeError, ValueError):
                                auto_close_hold_s = 0.6
                            break
        if auto_close and grip != "close":
            grip = "close"
            out["gripper_command_source"] = "metric_plan_autoclose"
            out["interpreted"]["gripper_command"] = "close"
            out["gripper_command"] = "close"

        if motion.get("ok") and grip == "close":
            try:
                ang = float((os.environ.get("GO2_GRASP_COACH_GRIPPER_CLOSE_DEG") or "-14").strip())
            except ValueError:
                ang = -14.0
            grip_out = publish_move_one_joint_deg(6, ang)
            if grip_out is not None and grip_out.get("ok"):
                from go2_dashboard.grasp_close_verify import verify_gripper_grasp

                verify = verify_gripper_grasp(ang, hold_s=auto_close_hold_s)
                out["grasp_verify"] = verify
                if metric_info is not None:
                    metric_info["grasp_verify"] = verify
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
                "gripper_command_source": out.get("gripper_command_source"),
                "grasp_verify": out.get("grasp_verify"),
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

    return out


def _grasp_coach_preview_light(*, instruction: str, out: dict[str, Any]) -> dict[str, Any]:
    """Capture polso + detect + snapshot JPEG — senza piano IK (veloce per «Foto SDK»)."""
    import sys
    import time

    from go2_dashboard.orbbec_wrist_grasp import _filter_wrist_detection, _wrist_debug_tag, capture_aligned
    from go2_dashboard.paths import PROJECT_ROOT

    t0 = time.perf_counter()
    cap = capture_aligned()
    out["capture_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    if not cap.get("ok"):
        out["reason"] = cap.get("reason", "capture_failed")
        out["hint_it"] = cap.get("hint_it") or cap.get("detail")
        out["label_it"] = "Acquisizione SDK fallita — attendi 2 s e riprova."
        return out
    out["depth_nonzero_px"] = cap.get("depth_nonzero_px")
    s = str(PROJECT_ROOT / "scripts")
    if s not in sys.path:
        sys.path.insert(0, s)
    from box_object_detector import detect_box_object, parse_color_from_instruction

    hint = parse_color_from_instruction(instruction or "")
    color = cap["color_bgr"]
    intr = cap["intrinsics"]
    det = _filter_wrist_detection(detect_box_object(color, color_hint=hint), intr)
    debug_snap: dict[str, Any] = {}
    try:
        from go2_dashboard.grasp_detect_debug import save_detection_snapshot

        debug_snap = save_detection_snapshot(
            color,
            det if isinstance(det, dict) else None,
            tag=_wrist_debug_tag(),
            logical_camera=0,
            step="wrist_snapshot_light",
        )
    except Exception as exc:
        debug_snap = {"saved": False, "error": repr(exc)}
    out["debug_snapshot"] = debug_snap
    if debug_snap.get("image_url"):
        out["metric_viz_url"] = str(debug_snap["image_url"])
    out["object_detection"] = det
    partial = bool(det.get("ok"))
    out["object_visible"] = partial
    out["ok"] = partial
    out["reason"] = "" if partial else str(det.get("reason") or "no_detection")
    out["label_it"] = (
        "SDK OK — oggetto rilevato."
        if partial
        else f"SDK acquisito, nessun oggetto ({out['reason']}) — metti la scatola in vista log.0."
    )
    return out


def grasp_coach_preview_metric(
    *,
    instruction: str = "",
    start_variant: str | None = None,
    light: bool = False,
) -> dict[str, Any]:
    """Anteprima presa dalla posa servo attuale (metrico polso) — senza OpenAI e senza movimento.

    ``light=True``: solo capture SDK + detect + JPEG debug (~3–5 s), senza IK completa.
    """
    lateral = normalize_start_variant(start_variant) == START_VARIANT_LATERAL or _lateral_grasp_mode(
        {"start_variant": start_variant}
    )
    out: dict[str, Any] = {
        "ok": False,
        "preview_only": True,
        "instruction": instruction,
        "object_visible": False,
        "interpreted": {},
        "start_variant": normalize_start_variant(start_variant),
        "lateral_grasp_mode": lateral,
        "light": bool(light),
    }
    if not go2_local():
        out["reason"] = "not_go2_local"
        return out
    if light:
        return _grasp_coach_preview_light(instruction=instruction, out=out)
    try:
        from go2_dashboard.d1_arm_publish_lite import current_tool_tip_base_link_m
        from go2_dashboard.d1_servo_feedback import read_servo_deg_with_diag
        from go2_dashboard.orbbec_wrist_grasp import plan_wrist_grasp_metric

        cur_tip, _ = current_tool_tip_base_link_m()
        servo_now, _sdiag = read_servo_deg_with_diag(PROJECT_ROOT)
        if servo_now is None or len(servo_now) < 6:
            out["reason"] = "no_servo_feedback"
            return out
        mp = plan_wrist_grasp_metric([float(x) for x in servo_now[:7]], instruction=instruction)
        det = mp.get("object_detection") if isinstance(mp.get("object_detection"), dict) else {}
        stages_dbg: list[dict[str, Any]] = []
        preview = mp.get("preview") if isinstance(mp.get("preview"), dict) else {}
        for st in (preview.get("plan") or []):
            if not isinstance(st, dict):
                continue
            s_tgt = st.get("target_xyz_m")
            s_nogo = (
                None
                if lateral
                else (
                    _nogo_zone_reason([float(v) for v in s_tgt])
                    if isinstance(s_tgt, (list, tuple)) and len(s_tgt) >= 3
                    else None
                )
            )
            stages_dbg.append({
                "stage": st.get("stage"),
                "target_xyz_m": s_tgt,
                "ik_ok": bool(st.get("ik_ok")),
                "collision_dog": s_nogo,
                "safe": bool(st.get("ik_ok") and s_nogo is None),
            })
        metric_info: dict[str, Any] = {
            "ok": bool(mp.get("ok")),
            "reason": mp.get("reason"),
            "grasp_display_base_link_m": mp.get("grasp_display_base_link_m"),
            "label": det.get("label"),
            "confidence": det.get("confidence"),
            "backend": det.get("backend"),
            "bbox_xyxy": det.get("bbox_xyxy"),
            "bbox_center_px": det.get("bbox_center_px"),
            # Asse di presa = asse REALE del pezzo (minAreaRect) se disponibile.
            "grip_axis_px": det.get("orient_axis_px") or det.get("grip_axis_px"),
            "orientation_deg": det.get("orientation_deg"),
            "frame_size_px": det.get("frame_size_px"),
            "depth_m": mp.get("depth_m"),
            "reach_m": mp.get("reach_m"),
            "reachable": mp.get("reachable"),
            "ik_stages": stages_dbg,
            "teach_calib_applied": bool(mp.get("teach_calib_applied")),
            "teach_calib_sample_id": mp.get("teach_calib_sample_id"),
            "teach_calib_delta_servo_deg": mp.get("teach_calib_delta_servo_deg"),
        }
        snap = mp.get("debug_snapshot") if isinstance(mp.get("debug_snapshot"), dict) else {}
        if snap.get("image_url"):
            metric_info["viz_image_url"] = str(snap["image_url"])
        elif mp.get("metric_viz_url"):
            metric_info["viz_image_url"] = str(mp["metric_viz_url"])
        if mp.get("hint_it"):
            metric_info["hint_it"] = str(mp["hint_it"])
        if mp.get("partial_rgb_ok") and det.get("ok"):
            metric_info["partial_rgb_ok"] = True
            metric_info["ok"] = False
            metric_info["reason"] = mp.get("reason")
        coach_tgt = None
        if mp.get("ok"):
            coach_tgt, _coach_stage = _coach_target_from_metric_plan(mp, cur_tip, lateral=lateral)
        object_pixel = None
        ctr = det.get("bbox_center_px")
        fsz = det.get("frame_size_px")
        if (
            isinstance(ctr, (list, tuple))
            and len(ctr) >= 2
            and isinstance(fsz, (list, tuple))
            and len(fsz) >= 2
            and float(fsz[0]) > 0
            and float(fsz[1]) > 0
        ):
            object_pixel = [
                max(0.0, min(1.0, float(ctr[0]) / float(fsz[0]))),
                max(0.0, min(1.0, float(ctr[1]) / float(fsz[1]))),
            ]
        partial_rgb = bool(mp.get("partial_rgb_ok")) and bool(det.get("ok"))
        out["ok"] = bool(mp.get("ok"))
        out["reason"] = mp.get("reason") if not mp.get("ok") else ""
        out["object_visible"] = bool(mp.get("ok")) or partial_rgb
        out["object_pixel_norm"] = object_pixel
        out["metric_grounding"] = metric_info
        out["target_source"] = "metric_orbbec" if mp.get("ok") else None
        if metric_info.get("viz_image_url"):
            out["metric_viz_url"] = metric_info["viz_image_url"]
        elif mp.get("metric_viz_url"):
            out["metric_viz_url"] = str(mp["metric_viz_url"])
        out["interpreted"] = {
            "target_xyz_base_link_m": coach_tgt,
            "object_visible": out["object_visible"],
            "object_pixel_norm": object_pixel,
            "tool_tip_base_link_m_now": cur_tip,
            "target_source": out["target_source"],
            "nogo_blocked": False,
        }
        try:
            from go2_dashboard.operator_plan_cache import set_last_grasp_plan

            if isinstance(mp, dict) and mp.get("grasp_display_base_link_m"):
                set_last_grasp_plan(mp)
        except Exception:
            pass
        if mp.get("ok"):
            out["label_it"] = "Anteprima OK: oggetto visto dal polso, traiettoria IK calcolata."
        elif partial_rgb:
            out["label_it"] = (
                "RGB polso OK, depth insufficiente ("
                + str(mp.get("reason") or "depth")
                + ") — premi «Foto SDK (metrica)» o avvicina la scatola."
            )
        else:
            out["label_it"] = f"Anteprima: nessun oggetto metrico ({mp.get('reason') or 'unknown'})."
    except Exception as exc:  # noqa: BLE001
        out["reason"] = "preview_error"
        out["detail"] = repr(exc)
    return out

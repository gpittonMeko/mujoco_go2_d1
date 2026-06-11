from __future__ import annotations

from datetime import datetime
from typing import Any

from go2_dashboard.d1_arm_publish_lite import editor_max_step_deg_7, parse_step_deg_csv

def _arm_post_delay_ms(body: dict[str, Any]) -> int | None:
    delay_ms = body.get("delay_ms")
    try:
        return int(delay_ms) if delay_ms is not None and str(delay_ms).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _op_now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _true_zero_motion_http_response_lite(result: dict[str, Any]) -> tuple[dict[str, Any], int]:
    if result.get("skipped"):
        payload = {
            **result,
            "ok": False,
            "hint": (
                "Nessun comando DDS inviato. Su NX: GO2_ENABLE_REAL_ARM=1; per goto da file serve anche "
                "uno tra GO2_ENABLE_ARM_PLAN_EXECUTE / GO2_ENABLE_OPENVLA_ARM_EXECUTE / GO2_ENABLE_GRASP_IK_EXECUTE."
            ),
        }
        return payload, 503
    ok = bool(result.get("ok"))
    out = {**result, "ok": ok}
    return out, 200 if ok else 502


def _parse_goto_max_step_deg(body: dict[str, Any]) -> list[float] | None:
    custom = body.get("max_step_deg")
    if isinstance(custom, str) and custom.strip():
        return parse_step_deg_csv(custom, editor_max_step_deg_7())
    return None

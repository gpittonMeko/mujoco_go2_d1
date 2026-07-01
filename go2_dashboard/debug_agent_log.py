"""NDJSON debug log per sessione agent (lab). Scrittura append-only su data/debug-*.ndjson."""

from __future__ import annotations

import json
import time
from typing import Any

from go2_dashboard.paths import PROJECT_ROOT

_SESSION_ID = "16a61f"
_LOG_PATH = PROJECT_ROOT / "data" / f"debug-{_SESSION_ID}.ndjson"


def agent_log_path() -> str:
    return str(_LOG_PATH)


def dbg_agent_log(
    location: str,
    message: str,
    data: dict[str, Any] | None = None,
    *,
    hypothesis_id: str = "",
    run_id: str = "scan-start-v2",
) -> None:
    # #region agent log
    try:
        payload = {
            "sessionId": _SESSION_ID,
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
            "timestamp": int(time.time() * 1000),
        }
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass
    # #endregion

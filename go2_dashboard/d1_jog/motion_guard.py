"""Un solo piano di controllo alla volta (giunti OR cartesiano OR admin)."""

from __future__ import annotations

import threading
import time

_lock = threading.RLock()
_kind: str = "idle"
_plane: str = "idle"  # idle | joint | cartesian
_safety_preempt: bool = False
_safety_source: str | None = None
_safety_started_at: float | None = None
_battery_lock: bool = False
_battery_lock_reason: str | None = None


def status() -> dict[str, str | bool]:
    with _lock:
        return {
            "busy": _kind != "idle",
            "kind": _kind,
            "plane": _plane,
            "joint_locked": _plane == "joint",
            "cartesian_locked": _plane == "cartesian",
            "safety_preempt": _safety_preempt,
            "safety_source": _safety_source,
            "safety_started_at": _safety_started_at,
            "battery_lock": _battery_lock,
            "battery_lock_reason": _battery_lock_reason,
        }


def begin_safety_preempt(source: str) -> None:
    global _kind, _plane, _safety_preempt, _safety_source, _safety_started_at
    with _lock:
        _safety_preempt = True
        _safety_source = source
        _safety_started_at = time.time()
        _kind = "idle"
        _plane = "idle"


def end_safety_preempt(*, source: str | None = None) -> None:
    global _safety_preempt, _safety_source, _safety_started_at
    with _lock:
        if source is not None and _safety_source not in (None, source):
            return
        _safety_preempt = False
        _safety_source = None
        _safety_started_at = None


def safety_preempt_active() -> bool:
    with _lock:
        return bool(_safety_preempt)


def set_battery_lock(reason: str) -> None:
    """Blocca jog/programmi braccio finché la batteria non risale sopra la soglia clear."""
    global _battery_lock, _battery_lock_reason, _kind, _plane
    with _lock:
        _battery_lock = True
        _battery_lock_reason = reason
        _kind = "idle"
        _plane = "idle"


def clear_battery_lock(*, reason: str | None = None) -> None:
    global _battery_lock, _battery_lock_reason
    with _lock:
        _battery_lock = False
        _battery_lock_reason = reason


def battery_lock_active() -> bool:
    with _lock:
        return bool(_battery_lock)


def claim_plane(plane: str) -> tuple[bool, str | None]:
    """Blocca l'altro piano di controllo (slider vs frecce TCP)."""
    global _plane
    if plane not in ("joint", "cartesian"):
        return False, "invalid_plane"
    with _lock:
        if _battery_lock and plane in {"joint", "cartesian"}:
            return False, f"battery_lock:{_battery_lock_reason or 'low_soc'}"
        if _plane not in ("idle", plane):
            return False, f"plane_busy:{_plane}"
        _plane = plane
        return True, None


def release_plane(plane: str) -> None:
    global _plane
    with _lock:
        if _plane == plane:
            _plane = "idle"


def try_acquire(kind: str) -> tuple[bool, str | None]:
    global _kind
    allowed = {"joint", "cartesian", "zero", "admin", "hold", "program"}
    if kind not in allowed:
        return False, "invalid_motion_kind"
    with _lock:
        # In low-battery lock: solo hold/admin/zero (ripriega), niente jog/programmi.
        if _battery_lock and kind not in {"hold", "admin", "zero"}:
            return False, f"battery_lock:{_battery_lock_reason or 'low_soc'}"
        if _safety_preempt and kind not in {"hold", "admin"}:
            return False, f"motion_preempted:{_safety_source or 'safety'}"
        if _plane not in ("idle", kind):
            return False, f"plane_busy:{_plane}"
        if _kind not in ("idle", kind):
            return False, f"motion_busy:{_kind}"
        _kind = kind
        return True, None


def release(kind: str) -> None:
    global _kind
    with _lock:
        if _kind == kind:
            _kind = "idle"


def force_idle() -> None:
    global _kind, _plane
    with _lock:
        _kind = "idle"
        _plane = "idle"

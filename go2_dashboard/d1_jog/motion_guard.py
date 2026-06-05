"""Un solo piano di controllo alla volta (giunti OR cartesiano OR admin)."""

from __future__ import annotations

import threading

_lock = threading.RLock()
_kind: str = "idle"
_plane: str = "idle"  # idle | joint | cartesian


def status() -> dict[str, str | bool]:
    with _lock:
        return {
            "busy": _kind != "idle",
            "kind": _kind,
            "plane": _plane,
            "joint_locked": _plane == "joint",
            "cartesian_locked": _plane == "cartesian",
        }


def claim_plane(plane: str) -> tuple[bool, str | None]:
    """Blocca l'altro piano di controllo (slider vs frecce TCP)."""
    global _plane
    if plane not in ("joint", "cartesian"):
        return False, "invalid_plane"
    with _lock:
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

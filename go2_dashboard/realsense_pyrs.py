"""Intel RealSense via pyrealsense2: colore BGR + depth Z16 + IR (allineati)."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

_lock = threading.Lock()
_pipeline: Any = None
_align: Any = None
_started = False
_last_error: str | None = None
_frame_count = 0
_streams_enabled: list[str] = []
_last_bundle: dict[str, Any] | None = None


@dataclass
class RsFrameBundle:
    color: np.ndarray | None = None
    depth_mm: np.ndarray | None = None
    ir: np.ndarray | None = None
    ir2: np.ndarray | None = None
    ts: float = 0.0
    streams: list[str] = field(default_factory=list)


def _backend_enabled() -> bool:
    b = os.environ.get("GO2_REALSENSE_COLOR_BACKEND", "auto").strip().lower()
    if b in ("0", "false", "no", "off", "v4l", "v4l2"):
        return False
    return b in ("1", "true", "yes", "on", "pyrs", "pyrealsense", "auto")


def _parse_streams() -> list[str]:
    raw = os.environ.get("GO2_REALSENSE_STREAMS", "color,depth,ir,ir2").strip().lower()
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        p = part.strip()
        if p in ("color", "rgb", "bgr"):
            if "color" not in out:
                out.append("color")
        elif p in ("depth", "z", "z16"):
            if "depth" not in out:
                out.append("depth")
        elif p in ("ir", "ir1", "infrared", "infra"):
            if "ir" not in out:
                out.append("ir")
        elif p in ("ir2", "infrared2"):
            if "ir2" not in out:
                out.append("ir2")
    return out or ["color"]


def status() -> dict[str, Any]:
    return {
        "enabled": _backend_enabled(),
        "started": _started,
        "frames": _frame_count,
        "error": _last_error,
        "backend": "pyrealsense2" if _started else None,
        "streams": list(_streams_enabled),
        "has_last_bundle": _last_bundle is not None,
    }


def start() -> bool:
    """Avvia pipeline RealSense (color + opz. depth/IR allineati al colore)."""
    global _pipeline, _align, _started, _last_error, _frame_count, _streams_enabled
    if not _backend_enabled():
        return False
    with _lock:
        if _started and _pipeline is not None:
            return True
        try:
            import pyrealsense2 as rs

            w = int(os.environ.get("GO2_REALSENSE_COLOR_WIDTH", "640"))
            h = int(os.environ.get("GO2_REALSENSE_COLOR_HEIGHT", "480"))
            fps = int(os.environ.get("GO2_REALSENSE_COLOR_FPS", "15"))
            streams = _parse_streams()
            cfg = rs.config()
            cfg.enable_stream(rs.stream.color, w, h, rs.format.bgr8, fps)
            if "depth" in streams:
                cfg.enable_stream(rs.stream.depth, w, h, rs.format.z16, fps)
            if "ir" in streams:
                cfg.enable_stream(rs.stream.infrared, 1, w, h, rs.format.y8, fps)
            if "ir2" in streams:
                cfg.enable_stream(rs.stream.infrared, 2, w, h, rs.format.y8, fps)
            pipe = rs.pipeline()
            pipe.start(cfg)
            _pipeline = pipe
            _align = rs.align(rs.stream.color) if "depth" in streams else None
            _streams_enabled = streams
            _started = True
            _last_error = None
            _frame_count = 0
            return True
        except Exception as exc:
            _pipeline = None
            _align = None
            _started = False
            _streams_enabled = []
            _last_error = f"{type(exc).__name__}: {exc}"
            return False


def stop() -> None:
    global _pipeline, _align, _started, _streams_enabled, _last_bundle
    with _lock:
        if _pipeline is not None:
            try:
                _pipeline.stop()
            except Exception:
                pass
        _pipeline = None
        _align = None
        _started = False
        _streams_enabled = []
        _last_bundle = None


def peek_bundle() -> dict[str, Any] | None:
    with _lock:
        return dict(_last_bundle) if _last_bundle else None


def read_bundle(timeout_ms: int | None = None) -> RsFrameBundle | None:
    """Frame allineati: color BGR, depth uint16 mm, IR uint8."""
    global _frame_count, _last_error, _last_bundle, _pipeline, _align, _started
    if not _started or _pipeline is None:
        if not start():
            return None
    wait_ms = int(timeout_ms or os.environ.get("GO2_REALSENSE_FRAME_TIMEOUT_MS", "8000"))
    with _lock:
        if _pipeline is None:
            return None
        try:
            import pyrealsense2 as rs

            frames = _pipeline.wait_for_frames(wait_ms)
            if _align is not None:
                frames = _align.process(frames)
            cf = frames.get_color_frame()
            if not cf:
                _last_error = "no_color_frame"
                return None
            color = np.asanyarray(cf.get_data())
            if color.ndim == 2:
                import cv2

                color = cv2.cvtColor(color, cv2.COLOR_GRAY2BGR)

            depth_mm: np.ndarray | None = None
            if "depth" in _streams_enabled:
                df = frames.get_depth_frame()
                if df:
                    depth_mm = np.asanyarray(df.get_data()).astype(np.uint16)

            ir_img: np.ndarray | None = None
            ir2_img: np.ndarray | None = None
            if "ir" in _streams_enabled:
                irf = frames.get_infrared_frame(1)
                if irf:
                    ir_img = np.asanyarray(irf.get_data())
            if "ir2" in _streams_enabled:
                irf2 = frames.get_infrared_frame(2)
                if irf2:
                    ir2_img = np.asanyarray(irf2.get_data())

            bundle = RsFrameBundle(
                color=color,
                depth_mm=depth_mm,
                ir=ir_img,
                ir2=ir2_img,
                ts=time.time(),
                streams=list(_streams_enabled),
            )
            _last_bundle = {
                "color": color,
                "depth_mm": depth_mm,
                "ir": ir_img,
                "ir1": ir_img,
                "ir2": ir2_img,
                "ts": bundle.ts,
                "streams": bundle.streams,
            }
            _frame_count += 1
            _last_error = None
            return bundle
        except Exception as exc:
            _last_error = f"{type(exc).__name__}: {exc}"
            try:
                if _pipeline is not None:
                    _pipeline.stop()
            except Exception:
                pass
            _pipeline = None
            _align = None
            _started = False
            return None


def read_bgr(timeout_ms: int | None = None) -> np.ndarray | None:
    """Compat: solo colore BGR."""
    b = read_bundle(timeout_ms)
    return b.color if b is not None else None


def warmup(frames: int = 8) -> bool:
    ok = 0
    for _ in range(max(1, frames)):
        b = read_bundle()
        if b is not None and b.color is not None and b.color.size:
            ok += 1
        time.sleep(0.05)
    return ok >= 2

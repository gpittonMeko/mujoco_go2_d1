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


def _env_float(key: str, default: float) -> float:
    try:
        return float((os.environ.get(key) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = (os.environ.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_optional_float(key: str) -> float | None:
    raw = (os.environ.get(key) or "").strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _set_rs_option(sensor: Any, opt: Any, value: float | bool | None) -> None:
    if value is None:
        return
    try:
        if sensor.supports(opt):
            sensor.set_option(opt, float(value))
    except Exception:
        pass


def _apply_capture_sensor_options(profile: Any, rs: Any) -> None:
    """Apply optional NX env tuning before warmup frames.

    The dashboard RGB preview can look acceptable while the stereo module is
    overexposed or the laser is too weak for depth. Keep defaults unchanged
    unless explicit env overrides are present.
    """
    try:
        dev = profile.get_device()
        sensors = dev.query_sensors()
    except Exception:
        return
    for sensor in sensors:
        try:
            name = sensor.get_info(rs.camera_info.name)
        except Exception:
            name = ""
        if name == "RGB Camera":
            auto = os.environ.get("GO2_REALSENSE_RGB_AUTO_EXPOSURE")
            if auto is not None:
                _set_rs_option(sensor, rs.option.enable_auto_exposure, 1.0 if _env_bool("GO2_REALSENSE_RGB_AUTO_EXPOSURE", True) else 0.0)
            _set_rs_option(sensor, rs.option.exposure, _env_optional_float("GO2_REALSENSE_RGB_EXPOSURE"))
            _set_rs_option(sensor, rs.option.gain, _env_optional_float("GO2_REALSENSE_RGB_GAIN"))
            _set_rs_option(sensor, rs.option.brightness, _env_optional_float("GO2_REALSENSE_RGB_BRIGHTNESS"))
            _set_rs_option(sensor, rs.option.contrast, _env_optional_float("GO2_REALSENSE_RGB_CONTRAST"))
            _set_rs_option(sensor, rs.option.saturation, _env_optional_float("GO2_REALSENSE_RGB_SATURATION"))
        elif name == "Stereo Module":
            auto = os.environ.get("GO2_REALSENSE_STEREO_AUTO_EXPOSURE")
            if auto is not None:
                _set_rs_option(sensor, rs.option.enable_auto_exposure, 1.0 if _env_bool("GO2_REALSENSE_STEREO_AUTO_EXPOSURE", True) else 0.0)
            _set_rs_option(sensor, rs.option.exposure, _env_optional_float("GO2_REALSENSE_STEREO_EXPOSURE"))
            _set_rs_option(sensor, rs.option.gain, _env_optional_float("GO2_REALSENSE_STEREO_GAIN"))
            _set_rs_option(sensor, rs.option.laser_power, _env_optional_float("GO2_REALSENSE_LASER_POWER"))
            emitter = os.environ.get("GO2_REALSENSE_EMITTER_ENABLED")
            if emitter is not None:
                _set_rs_option(sensor, rs.option.emitter_enabled, 1.0 if _env_bool("GO2_REALSENSE_EMITTER_ENABLED", True) else 0.0)


def list_devices() -> list[dict[str, Any]]:
    """Elenco device RealSense collegati (serial + product_id) per inventario NX."""
    try:
        import pyrealsense2 as rs
    except Exception as exc:
        return [{"ok": False, "error": repr(exc)}]
    out: list[dict[str, Any]] = []
    for dev in rs.context().query_devices():
        try:
            usb = None
            try:
                if dev.supports(rs.camera_info.usb_type_descriptor):
                    usb = dev.get_info(rs.camera_info.usb_type_descriptor)
            except Exception:
                usb = None
            out.append(
                {
                    "serial": dev.get_info(rs.camera_info.serial_number),
                    "product_id": dev.get_info(rs.camera_info.product_id),
                    "name": dev.get_info(rs.camera_info.name),
                    "usb_type": usb,
                }
            )
        except Exception as exc:
            out.append({"ok": False, "error": repr(exc)})
    return out


def resolve_device_serial(role: str) -> str | None:
    """Serial pyrealsense2 per ``wrist`` (D456) o ``front`` (D435i)."""
    role = (role or "wrist").strip().lower()
    if role not in ("wrist", "front"):
        return None
    env_key = "GO2_WRIST_REALSENSE_SERIAL" if role == "wrist" else "GO2_FRONT_REALSENSE_SERIAL"
    serial = (os.environ.get(env_key) or "").strip()
    if serial:
        return serial
    try:
        import pyrealsense2 as rs
    except Exception:
        return None
    want_pid = (
        (os.environ.get("GO2_WRIST_REALSENSE_USB_PID") or "0b5c")
        if role == "wrist"
        else (os.environ.get("GO2_FRONT_REALSENSE_USB_PID") or "0b3a")
    ).strip().lower().replace("0x", "")
    matches: list[str] = []
    for dev in rs.context().query_devices():
        try:
            pid = dev.get_info(rs.camera_info.product_id).strip().lower().replace("0x", "")
            sn = dev.get_info(rs.camera_info.serial_number)
        except Exception:
            continue
        if pid == want_pid or pid.endswith(want_pid):
            matches.append(sn)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return matches[0]
    return None


def capture_aligned_on_demand(
    *,
    role: str = "wrist",
    timeout_ms: int | None = None,
    max_frames: int | None = None,
    fast: bool = False,
    force_full: bool = False,
    include_ir: bool = False,
) -> dict[str, Any]:
    """Capture on-demand color+depth allineati (pattern Orbbec: apri → frame → chiudi).

    Ritorna lo stesso contratto di ``orbbec_wrist_grasp.capture_aligned`` per la presa metrica.
    """
    role = (role or "wrist").strip().lower()
    if role not in ("wrist", "front"):
        return {"ok": False, "reason": "bad_role", "role": role}

    logical = 0 if role == "wrist" else 6
    use_fast = (not force_full) and (fast or _env_float("GO2_REALSENSE_CAPTURE_FAST", 0) >= 1)
    if use_fast:
        settle = _env_float("GO2_REALSENSE_CAPTURE_FAST_SETTLE_S", 0.35)
        pause_extra = _env_float("GO2_REALSENSE_CAPTURE_FAST_PAUSE_EXTRA_S", 0.0)
    else:
        settle = _env_float(
            "GO2_REALSENSE_CAPTURE_SETTLE_S",
            _env_float("GO2_ORBBEC_CAPTURE_SETTLE_S", 0.5),
        )
        pause_extra = _env_float("GO2_REALSENSE_CAPTURE_PAUSE_EXTRA_S", 0.35)
    pause_s = max(settle + pause_extra, _env_float("GO2_REALSENSE_CAPTURE_MIN_PAUSE_S", 0.35))
    try:
        from go2_dashboard.cameras import CAMERA_CACHE

        # A RealSense pipeline can need more than one second to negotiate its
        # first frame. Keep the competing V4L cache paused for the whole SDK
        # startup window, otherwise it reopens the device while wait_for_frames
        # is still running and the wrist color stream never starts.
        CAMERA_CACHE.request_pause(logical, duration_s=pause_s + _env_float("GO2_REALSENSE_CAPTURE_PAUSE_GUARD_S", 4.0))
    except Exception:
        pass
    stop()
    time.sleep(pause_s)

    serial = resolve_device_serial(role)
    if not serial:
        return {
            "ok": False,
            "reason": "realsense_device_not_found",
            "role": role,
            "hint_it": (
                f"Nessuna RealSense con PID atteso per ruolo {role}. "
                "Verifica USB e imposta GO2_WRIST_REALSENSE_SERIAL / GO2_FRONT_REALSENSE_SERIAL."
            ),
        }

    if timeout_ms is None:
        if use_fast:
            timeout_ms = int(_env_float("GO2_REALSENSE_CAPTURE_FAST_TIMEOUT_MS", 900))
        else:
            timeout_ms = int(
                _env_float(
                    "GO2_REALSENSE_CAPTURE_TIMEOUT_MS",
                    _env_float("GO2_ORBBEC_CAPTURE_TIMEOUT_MS", 2200),
                )
            )
    if max_frames is None:
        if use_fast:
            max_frames = int(_env_float("GO2_REALSENSE_CAPTURE_FAST_MAX_FRAMES", 6))
        else:
            max_frames = int(
                _env_float(
                    "GO2_REALSENSE_CAPTURE_MAX_FRAMES",
                    min(20.0, _env_float("GO2_ORBBEC_CAPTURE_MAX_FRAMES", 20)),
                )
            )

    t_capture0 = time.perf_counter()
    pipe: Any = None
    try:
        import pyrealsense2 as rs

        w = int(os.environ.get("GO2_REALSENSE_COLOR_WIDTH", "640"))
        h = int(os.environ.get("GO2_REALSENSE_COLOR_HEIGHT", "480"))
        fps = int(os.environ.get("GO2_REALSENSE_COLOR_FPS", "15"))
        pipe = rs.pipeline()
        cfg = rs.config()
        cfg.enable_device(serial)
        cfg.enable_stream(rs.stream.color, w, h, rs.format.bgr8, fps)
        cfg.enable_stream(rs.stream.depth, w, h, rs.format.z16, fps)
        if include_ir:
            cfg.enable_stream(rs.stream.infrared, 1, w, h, rs.format.y8, fps)
            cfg.enable_stream(rs.stream.infrared, 2, w, h, rs.format.y8, fps)
        align = rs.align(rs.stream.color)
        profile = pipe.start(cfg)
        _apply_capture_sensor_options(profile, rs)
        if use_fast:
            warmup = int(_env_float("GO2_REALSENSE_CAPTURE_FAST_WARMUP_DISCARD", 1))
        else:
            # Let D456 RGB auto-exposure converge before selecting the frame.
            # Four frames produced a nearly black wrist image in normal indoor
            # light even though later frames from the same sensor were usable.
            warmup = int(_env_float("GO2_REALSENSE_CAPTURE_WARMUP_DISCARD", 20))
        for _ in range(max(0, warmup)):
            try:
                pipe.wait_for_frames(200)
            except Exception:
                pass
        min_depth_nz = int(_env_float("GO2_REALSENSE_CAPTURE_MIN_DEPTH_NONZERO", 400))
        got: tuple[Any, Any] | None = None
        best: tuple[Any, Any, int] | None = None
        for _ in range(int(max_frames)):
            frames = pipe.wait_for_frames(int(timeout_ms))
            if not frames:
                continue
            frames = align.process(frames)
            cf = frames.get_color_frame()
            df = frames.get_depth_frame()
            if not cf or not df:
                continue
            depth_try = np.asanyarray(df.get_data())
            nz = int(np.count_nonzero(depth_try))
            if nz >= min_depth_nz:
                got = (cf, df)
                break
            if best is None or nz > best[2]:
                best = (cf, df, nz)
        if got is None and best is not None:
            got = (best[0], best[1])
        if got is None:
            return {"ok": False, "reason": "no_aligned_frame", "serial": serial, "role": role}
        cf, df = got
        color = np.asanyarray(cf.get_data())
        if color.ndim == 2:
            import cv2

            color = cv2.cvtColor(color, cv2.COLOR_GRAY2BGR)
        depth = np.asanyarray(df.get_data()).astype(np.uint16).copy()
        ir = None
        ir2 = None
        if include_ir:
            try:
                irf = frames.get_infrared_frame(1)
                if irf:
                    ir = np.asanyarray(irf.get_data()).copy()
            except Exception:
                ir = None
            try:
                irf2 = frames.get_infrared_frame(2)
                if irf2:
                    ir2 = np.asanyarray(irf2.get_data()).copy()
            except Exception:
                ir2 = None
        scale_mm = float(df.get_units()) * 1000.0
        depth_nz = int(np.count_nonzero(depth))
        intr = cf.profile.as_video_stream_profile().intrinsics
        return {
            "ok": True,
            "color_bgr": color,
            "depth_u16": depth,
            "ir_u8": ir,
            "ir2_u8": ir2,
            "depth_scale_mm": scale_mm,
            "depth_nonzero_px": depth_nz,
            "intrinsics": {
                "fx": float(intr.fx),
                "fy": float(intr.fy),
                "cx": float(intr.ppx),
                "cy": float(intr.ppy),
                "width": int(intr.width),
                "height": int(intr.height),
            },
            "backend": "pyrealsense2",
            "serial": serial,
            "role": role,
            "capture_ms": round((time.perf_counter() - t_capture0) * 1000.0, 1),
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": "realsense_capture_error",
            "detail": repr(exc),
            "serial": serial,
            "role": role,
        }
    finally:
        if pipe is not None:
            try:
                pipe.stop()
            except Exception:
                pass

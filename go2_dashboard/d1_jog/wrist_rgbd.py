"""Acquisizione metrica dalla RealSense D456 montata sul polso D1.

Un solo owner della pipeline: un thread sampler aggiorna `_last_frame`.
Debug / preview / ciclo pescano quel buffer (o chiedono un burst sul
sampler) invece di riaprire lo stream e bloccarsi a vicenda.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import threading
import time
from typing import Any

import numpy as np

from go2_dashboard.d1_jog import orbbec_capture


_capture_lock = threading.RLock()
_capture_active = threading.Event()
_last_status: dict[str, Any] = {"ok": None, "reason": "not_probed"}
_last_frame: "WristRgbdFrame | None" = None

_stream_thread: threading.Thread | None = None
_stream_stop = threading.Event()
_stream_ready = threading.Event()
_burst_lock = threading.Lock()
_burst_req: dict[str, Any] | None = None


@dataclass
class WristRgbdFrame:
    color_bgr: np.ndarray
    depth_m: np.ndarray
    intrinsics: dict[str, float | int]
    serial: str
    product_id: str
    depth_scale_m: float
    timestamp_s: float

    def public_info(self) -> dict[str, Any]:
        valid = np.isfinite(self.depth_m) & (self.depth_m > 0.0)
        return {
            "ok": True,
            "serial": self.serial,
            "product_id": self.product_id,
            "depth_scale_m": self.depth_scale_m,
            "shape_hw": [int(self.depth_m.shape[0]), int(self.depth_m.shape[1])],
            "depth_valid_fraction": round(float(np.mean(valid)), 4),
            "intrinsics": dict(self.intrinsics),
            "timestamp_s": self.timestamp_s,
            "source": "wrist_rgbd_stream",
            "age_s": round(max(0.0, time.time() - float(self.timestamp_s)), 3),
        }


def _device_identity(device: Any, rs: Any) -> tuple[str, str, str]:
    def get(info: Any) -> str:
        try:
            return str(device.get_info(info))
        except Exception:
            return ""

    return (
        get(rs.camera_info.serial_number),
        get(rs.camera_info.product_id).lower(),
        get(rs.camera_info.name),
    )


def _select_d456(rs: Any) -> tuple[Any | None, dict[str, Any]]:
    wanted_serial = (os.environ.get("D1_WRIST_RS_SERIAL") or "").strip()
    wanted_pid = (os.environ.get("D1_WRIST_RS_PRODUCT_ID") or "0b5c").strip().lower()
    seen: list[dict[str, str]] = []
    for dev in rs.context().query_devices():
        serial, pid, name = _device_identity(dev, rs)
        seen.append({"serial": serial, "product_id": pid, "name": name})
        if wanted_serial and serial == wanted_serial:
            return dev, {"serial": serial, "product_id": pid, "name": name, "seen": seen}
        if not wanted_serial and (pid == wanted_pid or "d456" in name.lower()):
            return dev, {"serial": serial, "product_id": pid, "name": name, "seen": seen}
    return None, {"wanted_serial": wanted_serial or None, "wanted_product_id": wanted_pid, "seen": seen}


def _stream_dimensions() -> tuple[int, int, int]:
    return (
        int(os.environ.get("D1_WRIST_RGBD_WIDTH", "640")),
        int(os.environ.get("D1_WRIST_RGBD_HEIGHT", "480")),
        int(os.environ.get("D1_WRIST_RGBD_FPS", "15")),
    )


def _frame_wait_ms() -> int:
    return max(500, int(os.environ.get("D1_WRIST_RGBD_WAIT_MS", "2000")))


def _build_frame(
    *,
    color: np.ndarray,
    depth_m: np.ndarray,
    color_profile: Any,
    serial: str,
    product_id: str,
    depth_scale: float,
) -> WristRgbdFrame:
    intr = color_profile.get_intrinsics()
    return WristRgbdFrame(
        color_bgr=color,
        depth_m=depth_m,
        intrinsics={
            "width": int(intr.width),
            "height": int(intr.height),
            "fx": float(intr.fx),
            "fy": float(intr.fy),
            "ppx": float(intr.ppx),
            "ppy": float(intr.ppy),
            "coeffs": [float(x) for x in intr.coeffs],
        },
        serial=serial,
        product_id=product_id,
        depth_scale_m=depth_scale,
        timestamp_s=time.time(),
    )


def _configure_depth_sensor(rs: Any, depth_sensor: Any) -> float:
    option_values = (
        (rs.option.visual_preset, float(os.environ.get("D1_WRIST_RS_VISUAL_PRESET", "4"))),
        (rs.option.emitter_enabled, float(os.environ.get("D1_WRIST_RS_EMITTER_ENABLED", "1"))),
    )
    for option, value in option_values:
        try:
            if depth_sensor.supports(option):
                depth_sensor.set_option(option, value)
        except Exception:
            pass
    try:
        if depth_sensor.supports(rs.option.laser_power):
            laser_range = depth_sensor.get_option_range(rs.option.laser_power)
            requested = float(os.environ.get("D1_WRIST_RS_LASER_POWER", str(laser_range.max)))
            depth_sensor.set_option(
                rs.option.laser_power,
                min(laser_range.max, max(laser_range.min, requested)),
            )
    except Exception:
        pass
    return float(depth_sensor.get_depth_scale())


def _median_depth(depths: list[np.ndarray]) -> np.ndarray:
    stack = np.stack(depths, axis=0)
    stack[stack <= 0.0] = np.nan
    with np.errstate(all="ignore"):
        depth_m = np.nanmedian(stack, axis=0).astype(np.float32)
    depth_m[~np.isfinite(depth_m)] = 0.0
    return depth_m


def _publish_frame(frame: WristRgbdFrame) -> None:
    global _last_status, _last_frame
    with _capture_lock:
        _last_frame = frame
        _last_status = frame.public_info()


def _stream_main() -> None:
    global _last_status, _burst_req
    wait_ms = _frame_wait_ms()
    pipe = None
    profile = None
    identity: dict[str, Any] = {}
    try:
        orbbec_capture.prepare_camera_for_snapshot()
        import pyrealsense2 as rs

        device, identity = _select_d456(rs)
        if device is None:
            _last_status = {"ok": False, "reason": "d456_not_found", **identity}
            return

        serial = str(identity["serial"])
        product_id = str(identity["product_id"])
        width, height, fps = _stream_dimensions()
        cfg = rs.config()
        cfg.enable_device(serial)
        cfg.enable_stream(rs.stream.depth, width, height, rs.format.z16, fps)
        cfg.enable_stream(rs.stream.color, width, height, rs.format.bgr8, fps)
        pipe = rs.pipeline()
        profile = pipe.start(cfg)
        _capture_active.set()
        align = rs.align(rs.stream.color)
        depth_scale = _configure_depth_sensor(rs, profile.get_device().first_depth_sensor())
        warm = max(2, int(os.environ.get("D1_WRIST_RGBD_WARMUP", "2")))
        for _ in range(warm):
            if _stream_stop.is_set():
                return
            try:
                pipe.wait_for_frames(wait_ms)
            except Exception as exc:
                _last_status = {
                    "ok": False,
                    "reason": "warmup_wait_failed",
                    "detail": str(exc),
                    **identity,
                }
                # Continua: a volte il primo frame arriva dopo un timeout.

        while not _stream_stop.is_set():
            req: dict[str, Any] | None = None
            with _burst_lock:
                if _burst_req is not None and not _burst_req.get("done"):
                    req = _burst_req

            n_frames = 1
            if req is not None:
                n_frames = max(1, int(req.get("median_frames", 3)))

            depths: list[np.ndarray] = []
            color: np.ndarray | None = None
            color_profile = None
            try:
                for _ in range(n_frames):
                    frames = align.process(pipe.wait_for_frames(wait_ms))
                    depth_frame = frames.get_depth_frame()
                    color_frame = frames.get_color_frame()
                    if not depth_frame or not color_frame:
                        continue
                    depths.append(np.asanyarray(depth_frame.get_data()).astype(np.float32) * depth_scale)
                    color = np.asanyarray(color_frame.get_data()).copy()
                    color_profile = color_frame.profile.as_video_stream_profile()
            except Exception as exc:
                _last_status = {
                    "ok": False,
                    "reason": type(exc).__name__,
                    "detail": str(exc),
                    **identity,
                }
                if req is not None:
                    with _burst_lock:
                        req["error"] = str(exc)
                        req["done"] = True
                        req["event"].set()
                        if _burst_req is req:
                            _burst_req = None
                time.sleep(0.05)
                continue

            if color is None or color_profile is None or not depths:
                if req is not None:
                    with _burst_lock:
                        req["error"] = "aligned_frames_unavailable"
                        req["done"] = True
                        req["event"].set()
                        if _burst_req is req:
                            _burst_req = None
                continue

            frame = _build_frame(
                color=color,
                depth_m=_median_depth(depths) if len(depths) > 1 else depths[0],
                color_profile=color_profile,
                serial=serial,
                product_id=product_id,
                depth_scale=depth_scale,
            )
            _publish_frame(frame)
            _stream_ready.set()

            if req is not None:
                with _burst_lock:
                    req["frame"] = frame
                    req["done"] = True
                    req["event"].set()
                    if _burst_req is req:
                        _burst_req = None
            else:
                # Idle ~3 Hz: abbastanza per live debug, senza saturare la NX.
                time.sleep(max(0.05, float(os.environ.get("D1_WRIST_RGBD_IDLE_SLEEP_S", "0.33"))))
    except Exception as exc:
        _last_status = {
            "ok": False,
            "reason": type(exc).__name__,
            "detail": str(exc),
            **identity,
        }
    finally:
        _stream_ready.clear()
        if profile is not None and pipe is not None:
            try:
                pipe.stop()
            except Exception:
                pass
        _capture_active.clear()
        with _burst_lock:
            if _burst_req is not None and not _burst_req.get("done"):
                _burst_req["error"] = "stream_stopped"
                _burst_req["done"] = True
                _burst_req["event"].set()
            _burst_req = None


def ensure_stream() -> None:
    """Avvia (una sola volta) il sampler continuo sulla D456."""
    global _stream_thread
    with _capture_lock:
        if _stream_thread is not None and _stream_thread.is_alive():
            return
        _stream_stop.clear()
        _stream_ready.clear()
        _stream_thread = threading.Thread(target=_stream_main, name="wrist-rgbd-stream", daemon=True)
        _stream_thread.start()


def stop_stream(*, join_timeout_s: float = 3.0) -> None:
    """Ferma il sampler (es. shutdown processo)."""
    global _stream_thread
    _stream_stop.set()
    th = _stream_thread
    if th is not None and th.is_alive():
        th.join(timeout=max(0.1, float(join_timeout_s)))
    with _capture_lock:
        if _stream_thread is th and (th is None or not th.is_alive()):
            _stream_thread = None


def last_frame(*, max_age_s: float | None = None) -> WristRgbdFrame | None:
    """Ultima RGB-D dal buffer; opzionalmente scarta se troppo vecchia."""
    with _capture_lock:
        frame = _last_frame
    if frame is None:
        return None
    if max_age_s is not None and (time.time() - float(frame.timestamp_s)) > float(max_age_s):
        return None
    return frame


def latest_or_capture(
    *,
    max_age_s: float = 0.8,
    median_frames: int = 1,
    wait_s: float = 3.0,
) -> WristRgbdFrame:
    """Preferisci il buffer; se stale/assente chiedi un burst al sampler."""
    ensure_stream()
    frame = last_frame(max_age_s=max_age_s)
    if frame is not None:
        return frame
    return capture_aligned(median_frames=median_frames, wait_s=wait_s)


def capture_aligned(
    *,
    warmup_frames: int | None = None,
    median_frames: int = 3,
    wait_s: float | None = None,
) -> WristRgbdFrame:
    """Burst sul sampler continuo (non riapre la pipeline da thread HTTP)."""
    del warmup_frames  # warmup gestito all'avvio dello stream
    global _burst_req
    ensure_stream()
    timeout = float(wait_s if wait_s is not None else os.environ.get("D1_WRIST_RGBD_BURST_WAIT_S", "8"))
    if not _stream_ready.wait(timeout=max(2.0, timeout)):
        # Mai aprire una seconda pipeline se il sampler e' vivo: e' la race
        # che inchiodava Flask (oneshot vs stream sulla stessa D456).
        th = _stream_thread
        if th is not None and th.is_alive():
            raise RuntimeError("rgbd_stream_not_ready")
        return _capture_aligned_oneshot(median_frames=median_frames)

    event = threading.Event()
    req: dict[str, Any] = {
        "median_frames": max(1, int(median_frames)),
        "event": event,
        "done": False,
        "frame": None,
        "error": None,
    }
    own_burst = False
    with _burst_lock:
        if _burst_req is None or _burst_req.get("done"):
            _burst_req = req
            own_burst = True

    if own_burst:
        if not event.wait(timeout=timeout):
            with _burst_lock:
                if _burst_req is req:
                    _burst_req = None
            raise RuntimeError("rgbd_burst_timeout")
        if req.get("error"):
            raise RuntimeError(str(req["error"]))
        frame = req.get("frame")
        if isinstance(frame, WristRgbdFrame):
            return frame
        raise RuntimeError("rgbd_burst_empty")

    # Qualcun altro aveva il burst: aspetta un frame fresco dal buffer.
    deadline = time.time() + timeout
    while time.time() < deadline:
        frame = last_frame(max_age_s=1.5)
        if frame is not None:
            return frame
        time.sleep(0.05)
    raise RuntimeError("rgbd_buffer_stale")


def _capture_aligned_oneshot(*, median_frames: int = 3) -> WristRgbdFrame:
    """Fallback: open/close una tantum se il sampler non parte."""
    global _last_status, _last_frame
    with _capture_lock:
        try:
            orbbec_capture.prepare_camera_for_snapshot()
            import pyrealsense2 as rs
        except Exception as exc:
            _last_status = {"ok": False, "reason": "pyrealsense2_unavailable", "detail": str(exc)}
            raise RuntimeError("pyrealsense2_unavailable") from exc

        device, identity = _select_d456(rs)
        if device is None:
            _last_status = {"ok": False, "reason": "d456_not_found", **identity}
            raise RuntimeError("d456_not_found")

        serial = str(identity["serial"])
        product_id = str(identity["product_id"])
        width, height, fps = _stream_dimensions()
        cfg = rs.config()
        cfg.enable_device(serial)
        cfg.enable_stream(rs.stream.depth, width, height, rs.format.z16, fps)
        cfg.enable_stream(rs.stream.color, width, height, rs.format.bgr8, fps)
        pipe = rs.pipeline()
        profile = None
        _capture_active.set()
        try:
            profile = pipe.start(cfg)
            align = rs.align(rs.stream.color)
            depth_scale = _configure_depth_sensor(rs, profile.get_device().first_depth_sensor())
            wait_ms = _frame_wait_ms()
            warm = max(2, int(os.environ.get("D1_WRIST_RGBD_WARMUP", "2")))
            for _ in range(warm):
                pipe.wait_for_frames(wait_ms)

            depths: list[np.ndarray] = []
            color: np.ndarray | None = None
            color_profile = None
            for _ in range(max(1, int(median_frames))):
                frames = align.process(pipe.wait_for_frames(wait_ms))
                depth_frame = frames.get_depth_frame()
                color_frame = frames.get_color_frame()
                if not depth_frame or not color_frame:
                    continue
                depths.append(np.asanyarray(depth_frame.get_data()).astype(np.float32) * depth_scale)
                color = np.asanyarray(color_frame.get_data()).copy()
                color_profile = color_frame.profile.as_video_stream_profile()
            if color is None or color_profile is None or not depths:
                raise RuntimeError("aligned_frames_unavailable")
            out = _build_frame(
                color=color,
                depth_m=_median_depth(depths),
                color_profile=color_profile,
                serial=serial,
                product_id=product_id,
                depth_scale=depth_scale,
            )
            _last_status = out.public_info()
            _last_frame = out
            return out
        except Exception as exc:
            _last_status = {
                "ok": False,
                "reason": type(exc).__name__,
                "detail": str(exc),
                **identity,
            }
            raise
        finally:
            if profile is not None:
                try:
                    pipe.stop()
                except Exception:
                    pass
            _capture_active.clear()


def health(*, capture: bool = False) -> dict[str, Any]:
    if capture:
        try:
            return latest_or_capture(max_age_s=1.0, median_frames=1).public_info()
        except Exception:
            return dict(_last_status)
    ensure_stream()
    info = dict(_last_status)
    info["stream_alive"] = bool(_stream_thread is not None and _stream_thread.is_alive())
    info["stream_ready"] = _stream_ready.is_set()
    return info


def capture_active() -> bool:
    """True mentre librealsense possiede la D456 del polso."""
    return _capture_active.is_set()


def try_acquire_camera() -> bool:
    """Lock condiviso con la preview UVC; non blocca i thread HTTP."""
    return _capture_lock.acquire(blocking=False)


def release_camera() -> None:
    _capture_lock.release()

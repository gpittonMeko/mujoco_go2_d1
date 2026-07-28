"""Acquisizione metrica dalla RealSense D456 montata sul polso D1.

Il modulo non pubblica comandi DDS. Prima di aprire librealsense ferma soltanto
la preview UVC della stessa camera, evitando due owner contemporanei.
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


def capture_aligned(*, warmup_frames: int | None = None, median_frames: int = 3) -> WristRgbdFrame:
    """Cattura RGB e depth allineata; la pipeline viene sempre chiusa."""
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
            depth_sensor = profile.get_device().first_depth_sensor()
            # La scatola stampata assorbe molto IR: High Density + laser massimo
            # aumenta il supporto senza applicare hole-filling geometrico.
            option_values = (
                # rs400_visual_preset: 1=Default, 4=High Density.
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
                    depth_sensor.set_option(rs.option.laser_power, min(laser_range.max, max(laser_range.min, requested)))
            except Exception:
                pass
            depth_scale = float(depth_sensor.get_depth_scale())
            warm = max(2, int(warmup_frames or os.environ.get("D1_WRIST_RGBD_WARMUP", "2")))
            for _ in range(warm):
                pipe.wait_for_frames(8000)

            depths: list[np.ndarray] = []
            color: np.ndarray | None = None
            color_profile = None
            for _ in range(max(1, int(median_frames))):
                frames = align.process(pipe.wait_for_frames(8000))
                depth_frame = frames.get_depth_frame()
                color_frame = frames.get_color_frame()
                if not depth_frame or not color_frame:
                    continue
                depths.append(np.asanyarray(depth_frame.get_data()).astype(np.float32) * depth_scale)
                color = np.asanyarray(color_frame.get_data()).copy()
                color_profile = color_frame.profile.as_video_stream_profile()
            if color is None or color_profile is None or not depths:
                raise RuntimeError("aligned_frames_unavailable")
            stack = np.stack(depths, axis=0)
            stack[stack <= 0.0] = np.nan
            with np.errstate(all="ignore"):
                depth_m = np.nanmedian(stack, axis=0).astype(np.float32)
            depth_m[~np.isfinite(depth_m)] = 0.0
            intr = color_profile.get_intrinsics()
            out = WristRgbdFrame(
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
            return capture_aligned(median_frames=2).public_info()
        except Exception:
            return dict(_last_status)
    return dict(_last_status)


def last_frame() -> WristRgbdFrame | None:
    """Restituisce l'ultima RGB-D allineata senza riaprire la D456."""
    with _capture_lock:
        return _last_frame


def capture_active() -> bool:
    """True mentre librealsense possiede la D456 del polso."""
    return _capture_active.is_set()


def try_acquire_camera() -> bool:
    """Lock condiviso con la preview UVC; non blocca i thread HTTP."""
    return _capture_lock.acquire(blocking=False)


def release_camera() -> None:
    _capture_lock.release()

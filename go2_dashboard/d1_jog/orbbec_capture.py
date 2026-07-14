"""Cattura / stream Orbbec RGB via V4L diretto per dashboard D1 5056."""

from __future__ import annotations

import os
import platform
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from pathlib import Path
from typing import Any

from go2_dashboard.cameras import (
    _USB_IDS_LOGICAL_0_ORBBEC,
    _USB_IDS_REALSENSE,
    _enumerate_v4l_usb_bindings,
    _frame_channel_chroma_bgr,
    _frame_looks_like_rgb_color,
    _try_set_uvc_mjpeg_fourcc,
    _v4l_sysfs_card_name,
)
from go2_dashboard.paths import PROJECT_ROOT

_SNAP_DIR = Path(
    os.environ.get(
        "D1_ORBBEC_SNAP_DIR",
        str(PROJECT_ROOT / "data" / "d1_orbbec_snaps"),
    )
)
_LATEST_NAME = "latest.jpg"
_cap_lock = threading.Lock()
_live_stop = threading.Event()
_resolved_rgb_idx: int | None = None
_live_cap: Any | None = None
_live_cap_idx: int | None = None
_live_ffmpeg_proc: subprocess.Popen[bytes] | None = None
_live_rgb_diag: dict[str, Any] = {"index": None, "chroma": None, "rgb_like": False}


def live_rgb_status() -> dict[str, Any]:
    return dict(_live_rgb_diag)


def _orbbec_frame_diagnostics(frame: Any, *, v4l_index: int | None = None) -> dict[str, Any]:
    """Diagnostica RGB con soglia Orbbec (non RealSense 2.5 — evita falsi positivi IR)."""
    chroma = _frame_channel_chroma_bgr(frame)
    spread = _bgr_mean_channel_spread(frame)
    spread_min = (
        _min_channel_spread_for_index(int(v4l_index))
        if v4l_index is not None
        else _orbbec_min_channel_spread()
    )
    rgb_like = _frame_looks_like_orbbec_rgb(frame, min_spread=spread_min)
    return {
        "color_chroma": round(float(chroma), 3) if frame is not None else None,
        "channel_spread": round(float(spread), 3) if frame is not None else None,
        "rgb_like": bool(rgb_like),
        "stream_kind": "rgb" if rgb_like else ("mono_or_ir" if chroma >= 0.0 else "unknown"),
        "min_chroma": _orbbec_min_frame_chroma(),
        "min_channel_spread": spread_min,
    }


def _release_live_ffmpeg() -> None:
    global _live_ffmpeg_proc
    proc = _live_ffmpeg_proc
    _live_ffmpeg_proc = None
    if proc is None:
        return
    try:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        pass


def _release_live_cap() -> None:
    global _live_cap, _live_cap_idx
    _release_live_ffmpeg()
    if _live_cap is not None:
        try:
            _live_cap.release()
        except Exception:
            pass
    _live_cap = None
    _live_cap_idx = None


def _v4l_sysfs_uvc_index(idx: int) -> int:
    """Indice UVC Gemini: 0=depth/meta, 1=IR, 2=RGB, 3=meta — usare solo 2 per colore."""
    if platform.system().lower() != "linux":
        return -1
    try:
        return int(Path(f"/sys/class/video4linux/video{int(idx)}/index").read_text().strip())
    except (OSError, ValueError):
        return -1


def _deny_uvc_indices() -> set[int]:
    """Opzionale: non usare sysfs UVC index 0 come deny globale (su Gemini 335Lg /dev/video6 può essere RGB)."""
    raw = (os.environ.get("D1_ORBBEC_DENY_UVC_INDEX") or "").strip()
    if not raw:
        return set()
    return set(_parse_index_list(raw))


def _preferred_uvc_index() -> int:
    raw = (os.environ.get("D1_ORBBEC_PREFERRED_UVC_INDEX") or "2").strip()
    try:
        return int(raw)
    except ValueError:
        return 2


def _use_ffmpeg_pipeline() -> bool:
    return os.environ.get("D1_ORBBEC_FFMPEG", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _ffmpeg_input_format() -> str:
    return (os.environ.get("D1_ORBBEC_FFMPEG_INPUT_FORMAT") or "mjpeg").strip() or "mjpeg"


def _min_jpeg_chroma() -> float:
    raw = (os.environ.get("D1_ORBBEC_MIN_JPEG_CHROMA") or "8").strip()
    try:
        return float(raw)
    except ValueError:
        return 8.0


def _jpeg_decode_metrics(jpeg: bytes) -> tuple[float, float] | None:
    """Metriche sul JPEG che vede il browser (non sul BGR grezzo in memoria)."""
    if not jpeg or len(jpeg) < 400 or jpeg[:2] != b"\xff\xd8":
        return None
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None or not getattr(img, "size", 0):
        return None
    return _frame_channel_chroma_bgr(img), _bgr_mean_channel_spread(img)


def _jpeg_passes_rgb_gate(jpeg: bytes, *, v4l_index: int | None = None) -> bool:
    """Gate finale sul frame servito (post-JPEG). Con RGB fissato: soglia bassa."""
    metrics = _jpeg_decode_metrics(jpeg)
    if metrics is None:
        return False
    chroma, spread = metrics
    pinned = _pinned_rgb_v4l_index()
    if pinned is not None and not _auto_discovery_enabled():
        if v4l_index is not None and int(v4l_index) != int(pinned):
            return False
        return chroma >= _min_jpeg_chroma() or spread >= float(
            os.environ.get("D1_ORBBEC_PINNED_MIN_CHANNEL_SPREAD", "0.04")
        )
    min_true_spread = float(os.environ.get("D1_ORBBEC_MIN_TRUE_COLOR_SPREAD", "0.8"))
    if spread < min_true_spread and chroma < _orbbec_min_frame_chroma():
        return False
    if chroma < _min_jpeg_chroma() and spread < min_true_spread:
        return False
    return spread >= min_true_spread or chroma >= _orbbec_min_frame_chroma()


def _reset_before_capture_enabled() -> bool:
    return os.environ.get("D1_ORBBEC_RESET_BEFORE_CAPTURE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _reload_uvc_on_reset() -> bool:
    return os.environ.get("D1_ORBBEC_RESET_RELOAD_UVC", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _fuser_kill_v4l(idx: int) -> None:
    dev = f"/dev/video{int(idx)}"
    if not Path(dev).exists():
        return
    try:
        subprocess.run(
            ["fuser", "-k", dev],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def reset_orbbec_camera(*, reload_uvc: bool | None = None) -> bool:
    """Libera la camera e ricarica UVC — chiamato prima di ogni foto per stabilità."""
    global _resolved_rgb_idx
    _live_stop.set()
    with _cap_lock:
        _release_live_cap()
    _resolved_rgb_idx = None

    pinned = _pinned_rgb_v4l_index()
    if pinned is not None:
        _fuser_kill_v4l(pinned)
    for idx in orbbec_all_v4l_indices():
        _fuser_kill_v4l(idx)

    do_reload = _reload_uvc_on_reset() if reload_uvc is None else bool(reload_uvc)
    if do_reload and platform.system().lower() == "linux":
        script = PROJECT_ROOT / "scripts" / "orbbec_reset_camera.sh"
        if script.is_file():
            try:
                subprocess.run(
                    ["bash", str(script)],
                    cwd=str(PROJECT_ROOT),
                    timeout=max(15, int(float(os.environ.get("D1_ORBBEC_RESET_TIMEOUT_S", "25")))),
                    check=False,
                    env=os.environ.copy(),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            pwd = (os.environ.get("GO2_NX_PASSWORD") or "123").strip()
            try:
                subprocess.run(
                    ["sudo", "-S", "sh", "-c", "modprobe -r uvcvideo 2>/dev/null; modprobe uvcvideo"],
                    input=(pwd + "\n").encode(),
                    timeout=20,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

    settle = float(os.environ.get("D1_ORBBEC_RESET_SETTLE_S", "2.5"))
    if settle > 0:
        time.sleep(settle)

    if pinned is not None:
        return _v4l_device_exists(pinned)
    return bool(orbbec_all_v4l_indices())


def prepare_camera_for_snapshot() -> None:
    """Ferma il live MJPEG; se abilitato, reset completo camera prima della foto."""
    if _reset_before_capture_enabled():
        reset_orbbec_camera()
        return
    _live_stop.set()
    with _cap_lock:
        _release_live_cap()
    settle = float(os.environ.get("D1_ORBBEC_SNAPSHOT_SETTLE_S", "0.35"))
    if settle > 0:
        time.sleep(settle)


def _operator_base() -> str:
    return (
        os.environ.get("D1_OPERATOR_URL")
        or os.environ.get("HERMES_OPERATOR_URL")
        or "http://127.0.0.1:5056"
    ).strip().rstrip("/")


def _rgb_only() -> bool:
    return os.environ.get("D1_ORBBEC_RGB_ONLY", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _parse_index_list(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


def _v4l_usb_ids(idx: int) -> tuple[str, str] | None:
    for vidx, vid, pid in _enumerate_v4l_usb_bindings():
        if int(vidx) == int(idx):
            return vid, pid
    return None


def _v4l_index_is_realsense_device(idx: int) -> bool:
    """Esclude Intel RealSense (camera frontale Go2) — non è Orbbec polso."""
    pair = _v4l_usb_ids(idx)
    if pair and pair in _USB_IDS_REALSENSE:
        return True
    name = _v4l_sysfs_card_name(idx).lower()
    return "realsense" in name or ("intel" in name and "depth" in name)


def _v4l_index_is_orbbec_device(idx: int) -> bool:
    """Solo nodi Orbbec Gemini (USB 2bc5:080b o nome sysfs)."""
    if _v4l_index_is_realsense_device(idx):
        return False
    pair = _v4l_usb_ids(idx)
    if pair and pair in _USB_IDS_LOGICAL_0_ORBBEC:
        return True
    name = _v4l_sysfs_card_name(idx).lower()
    return "orbbec" in name or "gemini" in name


def orbbec_usb_v4l_indices() -> list[int]:
    """Tutti i nodi V4L collegati a Orbbec Gemini (2bc5:080b)."""
    rows = _enumerate_v4l_usb_bindings()
    return sorted(
        {
            idx
            for idx, vid, pid in rows
            if (vid, pid) in _USB_IDS_LOGICAL_0_ORBBEC and not _v4l_index_is_realsense_device(idx)
        }
    )


def orbbec_sysfs_v4l_indices() -> list[int]:
    """Nodi V4L Orbbec da sysfs name (fallback se USB vid/pid non leggibile)."""
    if platform.system().lower() != "linux":
        return []
    base = Path("/sys/class/video4linux")
    if not base.is_dir():
        return []
    out: list[int] = []
    for node in sorted(base.glob("video*")):
        tail = node.name[5:]
        if not tail.isdigit():
            continue
        name = _v4l_sysfs_card_name(int(tail)).lower()
        if "orbbec" in name or "gemini" in name:
            out.append(int(tail))
    return sorted(set(out))


def orbbec_all_v4l_indices() -> list[int]:
    """Unione USB + sysfs — l'indice /dev/videoN cambia dopo reboot USB."""
    out = set(orbbec_usb_v4l_indices()) | set(orbbec_sysfs_v4l_indices())
    return sorted(i for i in out if _v4l_index_is_orbbec_device(i) and not _v4l_index_is_realsense_device(i))


def _v4l_device_exists(idx: int) -> bool:
    return Path(f"/dev/video{int(idx)}").exists()


def _auto_discovery_enabled() -> bool:
    return os.environ.get("D1_ORBBEC_AUTO_DISCOVERY", "0").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _allow_generic_rgb_fallback() -> bool:
    return os.environ.get("D1_PICK_ALLOW_GENERIC_RGB_FALLBACK", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _pinned_rgb_v4l_index() -> int | None:
    """Indice RGB fisso — live + capture usano solo questo /dev/videoN."""
    for key in ("D1_ORBBEC_RGB_V4L_INDEX", "D1_ORBBEC_LIVE_V4L_INDEX"):
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return None


def _fixed_rgb_v4l_index() -> int | None:
    return _pinned_rgb_v4l_index()


def _orbbec_min_frame_chroma() -> float:
    raw = (os.environ.get("D1_ORBBEC_MIN_FRAME_CHROMA") or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return max(
        float(os.environ.get("GO2_REALSENSE_MIN_FRAME_CHROMA", "2.5")),
        12.0,
    )


def _v4l_sysfs_name_is_ir(name: str) -> bool:
    """Esclude nodi il cui nome sysfs è IR/depth/infra (Gemini: video2 ≈ IR)."""
    n = (name or "").lower()
    if "rgb" in n or "color" in n or "colour" in n:
        return False
    if "ir" in n or "infra" in n or "infrared" in n:
        return True
    if "depth" in n:
        return True
    return False


def _v4l_sysfs_name_rgb_bonus(name: str) -> float:
    n = (name or "").lower()
    if "rgb" in n or "color" in n or "colour" in n:
        return 5000.0
    return 0.0


def _orbbec_min_channel_spread() -> float:
    raw = (os.environ.get("D1_ORBBEC_MIN_CHANNEL_SPREAD") or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return 8.0


def _auto_min_channel_spread() -> float:
    raw = (os.environ.get("D1_ORBBEC_AUTO_MIN_CHANNEL_SPREAD") or "0.05").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.05


def _min_channel_spread_for_index(idx: int) -> float:
    """Pinzato: soglia bassa; auto-discovery: accetta RGB con spread quasi nullo se chroma ok."""
    pinned = _fixed_rgb_v4l_index()
    if pinned is not None and int(idx) == int(pinned):
        raw = (os.environ.get("D1_ORBBEC_PINNED_MIN_CHANNEL_SPREAD") or "0.05").strip()
        try:
            return float(raw)
        except ValueError:
            return 0.05
    return _auto_min_channel_spread()


def _index_passes_rgb_gate(idx: int, spread: float, chroma: float) -> bool:
    """IR: spread≈0 e chroma bassa; RGB Orbbec può avere spread basso ma chroma alto."""
    if spread < 0 or chroma < 0:
        return False
    if chroma < _orbbec_min_frame_chroma():
        return False
    if spread >= _min_channel_spread_for_index(idx):
        return True
    # Scena uniforme: chroma alto ma spread minimo (es. video2 dopo re-enumeration USB).
    min_chroma_relaxed = _orbbec_min_frame_chroma() + float(
        os.environ.get("D1_ORBBEC_RELAXED_CHROMA_MARGIN", "4")
    )
    min_spread_floor = float(os.environ.get("D1_ORBBEC_MIN_SPREAD_FLOOR", "0.04"))
    return chroma >= min_chroma_relaxed and spread >= min_spread_floor


def _bgr_mean_channel_spread(frame: Any) -> float:
    """RGB vero: medie B/G/R distinte. IR/grayscale: spread≈0 (video4 falso positivo chroma)."""
    if frame is None or not getattr(frame, "size", 0):
        return 0.0
    b = float(frame[:, :, 0].mean())
    g = float(frame[:, :, 1].mean())
    r = float(frame[:, :, 2].mean())
    return max(abs(b - g), abs(g - r), abs(b - r))


def _frame_looks_like_orbbec_rgb(
    frame: Any,
    *,
    min_spread: float | None = None,
    v4l_index: int | None = None,
) -> bool:
    if not _frame_looks_like_rgb_color(frame):
        return False
    chroma = _frame_channel_chroma_bgr(frame)
    spread = _bgr_mean_channel_spread(frame)
    if v4l_index is not None:
        return _index_passes_rgb_gate(int(v4l_index), spread, chroma)
    spread_min = _orbbec_min_channel_spread() if min_spread is None else float(min_spread)
    if chroma < _orbbec_min_frame_chroma():
        return False
    return spread >= spread_min


def _v4l_deny_indices() -> set[int]:
    raw = (os.environ.get("D1_ORBBEC_V4L_DENY") or "0").strip()
    if not raw:
        return set()
    return set(_parse_index_list(raw))


def _v4l_indices_probe_order() -> list[int]:
    """Candidati Orbbec esistenti — preferenza env, poi auto su tutti i nodi Gemini."""
    deny = _v4l_deny_indices()
    all_orbbec = set(orbbec_all_v4l_indices())
    order: list[int] = []

    deny_uvc = _deny_uvc_indices()
    pref_uvc = _preferred_uvc_index()

    def _append(idx: int) -> None:
        if idx in deny or idx in order:
            return
        if not _v4l_device_exists(idx):
            return
        if not _v4l_index_is_orbbec_device(idx) or _v4l_index_is_realsense_device(idx):
            return
        if all_orbbec and idx not in all_orbbec:
            return
        uvc_idx = _v4l_sysfs_uvc_index(idx)
        if uvc_idx in deny_uvc:
            return
        if _v4l_sysfs_name_is_ir(_v4l_sysfs_card_name(idx)):
            return
        order.append(idx)

    for idx in sorted(all_orbbec):
        if _v4l_sysfs_uvc_index(idx) == pref_uvc:
            _append(idx)
    fixed = _fixed_rgb_v4l_index()
    if fixed is not None:
        _append(fixed)
        if order:
            return order
    manual = (os.environ.get("D1_ORBBEC_V4L_INDICES") or "").strip()
    preferred = _parse_index_list(
        manual or os.environ.get("D1_ORBBEC_RGB_V4L_PREFERRED", ""),
    )
    for idx in preferred:
        _append(idx)
    for idx in sorted(all_orbbec):
        _append(idx)
    return order


def _frame_score(frame: Any, *, sysfs_name: str = "", min_spread: float | None = None) -> tuple[float, bool]:
    rgb = _frame_looks_like_orbbec_rgb(frame, min_spread=min_spread)
    chroma = _frame_channel_chroma_bgr(frame)
    bright = float(frame.max()) if frame is not None and getattr(frame, "size", 0) else 0.0
    score = (
        _v4l_sysfs_name_rgb_bonus(sysfs_name)
        + (3000.0 if rgb else 0.0)
        + chroma * 15.0
        + bright * 0.12
    )
    return score, rgb


def _probe_index_rgb_quality(idx: int) -> tuple[float, float]:
    """(channel_spread, chroma) massimi — (-1,-1) se IR/mono/non apribile."""
    if platform.system().lower() != "linux" or not _v4l_device_exists(idx):
        return -1.0, -1.0
    if not _v4l_index_is_orbbec_device(idx) or _v4l_index_is_realsense_device(idx):
        return -1.0, -1.0
    if idx not in set(orbbec_all_v4l_indices()):
        return -1.0, -1.0
    if _v4l_sysfs_name_is_ir(_v4l_sysfs_card_name(idx)):
        return -1.0, -1.0
    try:
        import cv2
    except ImportError:
        return -1.0, -1.0
    cap = _open_v4l_cap(cv2, idx)
    if cap is None:
        return -1.0, -1.0
    best_spread = -1.0
    best_chroma = -1.0
    try:
        for _ in range(16):
            ok, fr = cap.read()
            if not ok or fr is None or not getattr(fr, "size", 0):
                continue
            best_spread = max(best_spread, _bgr_mean_channel_spread(fr))
            best_chroma = max(best_chroma, _frame_channel_chroma_bgr(fr))
    finally:
        cap.release()
    if best_spread < 0 or best_chroma < 0:
        return -1.0, -1.0
    if _rgb_only() and not _index_passes_rgb_gate(idx, best_spread, best_chroma):
        return -1.0, -1.0
    return best_spread, best_chroma


def _probe_index_rgb_chroma(idx: int) -> float:
    _spread, chroma = _probe_index_rgb_quality(idx)
    return chroma


def _verify_v4l_index_is_orbbec_rgb(idx: int) -> bool:
    spread, chroma = _probe_index_rgb_quality(idx)
    return _index_passes_rgb_gate(idx, spread, chroma)


def _pinned_index_usable(idx: int) -> bool:
    if not _v4l_device_exists(idx):
        return False
    if _allow_generic_rgb_fallback():
        if _ffmpeg_grab_jpeg(int(idx)) is not None:
            return True
        frame, _ = _read_rgb_frame(idx=idx)
        return frame is not None
    if not _v4l_index_is_orbbec_device(idx) or _v4l_index_is_realsense_device(idx):
        return False
    if _verify_v4l_index_is_orbbec_rgb(idx):
        return True
    trust = os.environ.get("D1_ORBBEC_TRUST_PINNED_RGB", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if not trust:
        return False
    if _ffmpeg_grab_jpeg(int(idx)) is not None:
        return True
    return False


def _pick_strict_orbbec_rgb_index(*, force_probe: bool = False) -> int | None:
    """RGB Orbbec: default indice FISSO (env). Auto-discovery solo se esplicitamente abilitata."""
    global _resolved_rgb_idx
    if platform.system().lower() != "linux":
        return None

    pinned = _pinned_rgb_v4l_index()
    if pinned is not None and _pinned_index_usable(pinned):
        _resolved_rgb_idx = pinned
        return pinned
    if pinned is not None and not _auto_discovery_enabled():
        _resolved_rgb_idx = None
        return None

    if (
        not force_probe
        and _resolved_rgb_idx is not None
        and _v4l_index_is_orbbec_device(_resolved_rgb_idx)
        and not _v4l_index_is_realsense_device(_resolved_rgb_idx)
        and _verify_v4l_index_is_orbbec_rgb(_resolved_rgb_idx)
    ):
        return _resolved_rgb_idx

    if pinned is not None and not _auto_discovery_enabled():
        return None

    best_idx: int | None = None
    best_score = -1.0
    candidates = _v4l_indices_probe_order() or sorted(orbbec_all_v4l_indices())
    pref_uvc = _preferred_uvc_index()
    uvc_bonus = float(os.environ.get("D1_ORBBEC_UVC_RGB_SCORE_BONUS", "1000"))
    spread_floor = float(os.environ.get("D1_ORBBEC_MIN_TRUE_COLOR_SPREAD_RELAXED", "0.04"))

    for idx in candidates:
        if not _v4l_index_is_orbbec_device(idx) or _v4l_index_is_realsense_device(idx):
            continue
        spread, chroma = _probe_index_rgb_quality(idx)
        if not _index_passes_rgb_gate(idx, spread, chroma):
            continue
        if spread < spread_floor:
            continue
        score = spread + (uvc_bonus if _v4l_sysfs_uvc_index(idx) == pref_uvc else 0.0)
        if score > best_score:
            best_score = score
            best_idx = idx

    _resolved_rgb_idx = best_idx
    return best_idx


def resolve_orbbec_rgb_v4l_index(*, force_probe: bool = False) -> int | None:
    """Sceglie /dev/videoN RGB Orbbec (chroma verificato — esclude IR)."""
    return _pick_strict_orbbec_rgb_index(force_probe=force_probe)


def _open_v4l_cap(cv2: Any, idx: int) -> Any | None:
    for opener in (
        lambda: cv2.VideoCapture(f"/dev/video{idx}", cv2.CAP_V4L2),
        lambda: cv2.VideoCapture(int(idx), cv2.CAP_V4L2),
    ):
        cap = opener()
        if cap is not None and cap.isOpened():
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            _try_set_uvc_mjpeg_fourcc(cap, prefer_env="GO2_ORBBEC_PREFER_MJPEG")
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            except Exception:
                pass
            return cap
        if cap is not None:
            cap.release()
    return None


def _read_rgb_frame(*, idx: int | None = None) -> tuple[Any | None, int | None]:
    if platform.system().lower() != "linux":
        return None, None
    try:
        import cv2
    except ImportError:
        return None, None

    use_idx = idx if idx is not None else resolve_orbbec_rgb_v4l_index()
    if use_idx is None:
        return None, None
    _resolved_rgb_idx = use_idx

    if (not _allow_generic_rgb_fallback()) and _v4l_sysfs_name_is_ir(_v4l_sysfs_card_name(use_idx)):
        return None, use_idx

    cap = _open_v4l_cap(cv2, use_idx)
    if cap is None:
        return None, use_idx
    sysfs_name = _v4l_sysfs_card_name(use_idx)
    spread_min = _min_channel_spread_for_index(use_idx)
    try:
        best_frame = None
        best_score = -1.0
        best_rgb = False
        for _ in range(16):
            ok, fr = cap.read()
            if not ok or fr is None or not getattr(fr, "size", 0):
                continue
            sc, rgb = _frame_score(fr, sysfs_name=sysfs_name, min_spread=spread_min)
            if _rgb_only() and not _frame_looks_like_orbbec_rgb(
                fr, min_spread=spread_min, v4l_index=use_idx
            ):
                continue
            if sc > best_score:
                best_score = sc
                best_rgb = rgb
                best_frame = fr
        if _rgb_only() and (best_frame is None or not best_rgb):
            return None, use_idx
        return best_frame, use_idx
    finally:
        cap.release()


def _encode_jpeg(frame: Any, *, v4l_index: int | None = None) -> bytes | None:
    try:
        import cv2
    except ImportError:
        return None
    quality = int(os.environ.get("D1_ORBBEC_JPEG_QUALITY", "98"))
    ok_enc, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok_enc or buf is None or len(buf) < 400:
        return None
    jpeg = buf.tobytes()
    if _rgb_only() and not _jpeg_passes_rgb_gate(jpeg, v4l_index=v4l_index):
        return None
    return jpeg


def _ffmpeg_grab_jpeg_once(idx: int, *, fmt: str) -> bytes | None:
    size = (os.environ.get("D1_ORBBEC_FFMPEG_SIZE") or "640x480").strip()
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "v4l2",
        "-input_format",
        fmt,
        "-video_size",
        size,
        "-i",
        f"/dev/video{int(idx)}",
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=14)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or len(proc.stdout) < 400:
        return None
    if _rgb_only() and not _jpeg_passes_rgb_gate(proc.stdout, v4l_index=idx):
        return None
    return proc.stdout


def _ffmpeg_grab_jpeg(idx: int) -> bytes | None:
    """Un JPEG dalla camera senza decode/encode OpenCV (preserva colore UVC)."""
    if platform.system().lower() != "linux" or not _use_ffmpeg_pipeline():
        return None
    warmup = max(0, int(os.environ.get("D1_ORBBEC_FFMPEG_WARMUP_FRAMES", "1")))
    formats = [f.strip() for f in (os.environ.get("D1_ORBBEC_FFMPEG_FORMATS") or "").split(",") if f.strip()]
    if not formats:
        formats = [_ffmpeg_input_format(), "yuyv422", "mjpeg"]
    seen: set[str] = set()
    for fmt in formats:
        if fmt in seen:
            continue
        seen.add(fmt)
        for _ in range(warmup):
            _ffmpeg_grab_jpeg_once(idx, fmt=fmt)
        jpeg = _ffmpeg_grab_jpeg_once(idx, fmt=fmt)
        if jpeg is not None:
            return jpeg
    return None


def _fetch_jpeg(url: str, *, timeout_s: float = 12.0) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "d1-jog-orbbec-capture/1"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if len(data) < 400:
                return None
            if "image" in ctype or data[:2] == b"\xff\xd8":
                return data
    except (urllib.error.URLError, OSError, TimeoutError):
        return None
    return None


def _operator_reachable() -> bool:
    try:
        urllib.request.urlopen(f"{_operator_base()}/api/health", timeout=2.5)
        return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _use_operator_http() -> bool:
    return os.environ.get("D1_ORBBEC_USE_OPERATOR_HTTP", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _operator_http_fallback() -> bool:
    return os.environ.get("D1_ORBBEC_HTTP_FALLBACK", "0").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _operator_pipeline_enabled() -> bool:
    """Dashboard D1 5056: default solo V4L RGB, HTTP solo se abilitato via env."""
    return _use_operator_http() or _operator_http_fallback()


def _candidate_http_urls() -> list[str]:
    """Solo preview V4L RGB espliciti — mai camera/0 (spesso IR su operator)."""
    base = _operator_base()
    urls: list[str] = []
    for idx in _v4l_indices_probe_order():
        urls.append(f"{base}/api/cameras/v4l/{idx}/preview.jpg")
    return urls


def _capture_rgb_v4l_index() -> int | None:
    pinned = _pinned_rgb_v4l_index()
    if pinned is not None and _allow_generic_rgb_fallback() and _v4l_device_exists(pinned):
        # Current wrist camera is an Intel RealSense D456. A pinned generic RGB
        # node is still validated by chroma/spread after capture; do not reject
        # it merely because this legacy module is named ``orbbec_capture``.
        return int(pinned)
    return _pick_strict_orbbec_rgb_index(force_probe=False)


def _capture_v4l_direct_jpeg() -> tuple[bytes, str, dict[str, Any]] | None:
    global _resolved_rgb_idx
    with _cap_lock:
        rgb_idx = _capture_rgb_v4l_index()
        if rgb_idx is None:
            return None
        idx = rgb_idx
        jpeg = _ffmpeg_grab_jpeg(idx)
        via = "ffmpeg_mjpeg"
        if jpeg is None:
            frame, idx = _read_rgb_frame(idx=rgb_idx)
            if frame is None or idx is None:
                _resolved_rgb_idx = None
                return None
            jpeg = _encode_jpeg(frame, v4l_index=idx)
            via = "opencv_bgr"
            if jpeg is None:
                _resolved_rgb_idx = None
                return None
        metrics = _jpeg_decode_metrics(jpeg)
        if metrics is None:
            _resolved_rgb_idx = None
            return None
        chroma, spread = metrics
        chroma = round(float(chroma), 2)
        spread = round(float(spread), 2)
        sysfs_name = _v4l_sysfs_card_name(idx)
        if (not _allow_generic_rgb_fallback()) and (
            not _v4l_index_is_orbbec_device(idx) or _v4l_index_is_realsense_device(idx)
        ):
            _resolved_rgb_idx = None
            return None
        meta = {
            "v4l_index": idx,
            "v4l_sysfs_name": sysfs_name,
            "usb_ids": _v4l_usb_ids(idx),
            "color_chroma": chroma,
            "channel_spread": spread,
            "stream_kind": "rgb",
            "uvc_index": _v4l_sysfs_uvc_index(idx),
            "via": via,
        }
        src = (
            f"v4l_rgb:/dev/video{idx} uvc={_v4l_sysfs_uvc_index(idx)} "
            f"({sysfs_name or 'no-name'} spread={spread} chroma={chroma})"
        )
        _resolved_rgb_idx = idx
        return jpeg, src, meta


def _save_jpeg(data: bytes, *, source: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    named = _SNAP_DIR / f"orbbec_{stamp}.jpg"
    named.write_bytes(data)
    latest = _SNAP_DIR / _LATEST_NAME
    latest.write_bytes(data)
    out: dict[str, Any] = {
        "ok": True,
        "source": source,
        "source_url": source if source.startswith("http") else None,
        "saved_at": stamp,
        "bytes": len(data),
        "filename": named.name,
        "image_url": f"/api/orbbec/last.jpg?t={int(time.time())}",
        "stream_kind": "rgb",
    }
    if extra:
        out.update(extra)
    return out


def _try_capture_once(*, tried: list[str], operator_up: bool) -> dict[str, Any] | None:
    allow_direct = os.environ.get("D1_ORBBEC_V4L_DIRECT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    def _from_v4l() -> dict[str, Any] | None:
        if not allow_direct:
            return None
        direct = _capture_v4l_direct_jpeg()
        if direct is None:
            tried.append("v4l_rgb:" + ",".join(str(i) for i in _v4l_indices_probe_order()))
            return None
        data, src, meta = direct
        extra: dict[str, Any] = {"via": "v4l_direct_rgb", **meta}
        return _save_jpeg(data, source=src, extra=extra)

    def _from_operator() -> dict[str, Any] | None:
        if not operator_up or not _use_operator_http():
            return None
        for url in _candidate_http_urls():
            tried.append(url)
            data = _fetch_jpeg(url)
            if data:
                return _save_jpeg(
                    data,
                    source=url,
                    extra={"via": "operator_http", "stream_kind": "rgb_http"},
                )
        return None

    got = _from_v4l()
    if got is not None:
        return got
    if operator_up and _operator_pipeline_enabled():
        return _from_operator()
    return None


def capture_orbbec_jpeg() -> dict[str, Any]:
    """Un frame Orbbec RGB — reset camera prima di ogni tentativo (stabile su NX)."""
    global _resolved_rgb_idx
    _SNAP_DIR.mkdir(parents=True, exist_ok=True)
    tried: list[str] = []
    operator_up = _operator_reachable() if _operator_pipeline_enabled() else False
    retries = max(1, int(os.environ.get("D1_ORBBEC_CAPTURE_RETRIES", "6")))
    allow_direct = os.environ.get("D1_ORBBEC_V4L_DIRECT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    for attempt in range(retries):
        _resolved_rgb_idx = None
        if _reset_before_capture_enabled():
            reset_orbbec_camera(reload_uvc=True)
        else:
            prepare_camera_for_snapshot()
        if attempt > 0:
            time.sleep(float(os.environ.get("D1_ORBBEC_CAPTURE_RETRY_DELAY_S", "0.8")))
        got = _try_capture_once(tried=tried, operator_up=operator_up)
        if got is not None and got.get("ok"):
            got["camera_reset"] = True
            if attempt > 0:
                got["capture_attempt"] = attempt + 1
            return got

    pinned = _pinned_rgb_v4l_index()
    orbbec_nodes = orbbec_all_v4l_indices()
    if pinned is not None and not _auto_discovery_enabled():
        hint_parts = [
            f"RGB Orbbec fissato su /dev/video{int(pinned)} — nodo assente o occupato. "
            f"Nodi visti: {', '.join(f'/dev/video{i}' for i in orbbec_nodes) or 'nessuno'}. "
            "Riavvia con scripts/nx_start_d1_jog.sh (reload UVC).",
        ]
    else:
        hint_parts = [
            "Nessun frame RGB Orbbec. Nodi Gemini: "
            + (", ".join(f"/dev/video{i}" for i in orbbec_nodes) if orbbec_nodes else "nessuno — controlla USB/cavo")
            + ".",
        ]
    if not allow_direct:
        hint_parts.append("Abilita D1_ORBBEC_V4L_DIRECT=1.")
    else:
        hint_parts.append(
            "Camera occupata? fuser -v /dev/video* e chiudi altri processi sul nodo RGB."
        )

    err: dict[str, Any] = {
        "ok": False,
        "reason": "orbbec_capture_failed",
        "rgb_only": _rgb_only(),
        "stream_kind": "rgb",
        "tried": tried,
        "hint": " ".join(hint_parts),
    }
    if _operator_pipeline_enabled():
        err["operator"] = _operator_base()
        err["operator_reachable"] = operator_up
    return err


def latest_snapshot_path() -> Path | None:
    p = _SNAP_DIR / _LATEST_NAME
    return p if p.is_file() else None


def _live_stream_v4l_index() -> int | None:
    """Indice live polso: usa lo stesso resolver della capture (supporta RealSense polso)."""
    return _capture_rgb_v4l_index()


def _open_live_cap(cv2: Any, idx: int) -> Any | None:
    """Apre /dev/videoN solo se il primo frame passa il test chroma RGB."""
    global _live_cap, _live_cap_idx
    if _live_cap is not None and _live_cap_idx == idx:
        return _live_cap
    _release_live_cap()
    if (not _allow_generic_rgb_fallback()) and (not _verify_v4l_index_is_orbbec_rgb(idx)):
        return None
    cap = _open_v4l_cap(cv2, idx)
    if cap is None:
        return None
    spread_min = _min_channel_spread_for_index(idx)
    rgb_ok = False
    for _ in range(12):
        ok, fr = cap.read()
        if not ok or fr is None or not getattr(fr, "size", 0):
            continue
        if _frame_looks_like_orbbec_rgb(fr, min_spread=spread_min, v4l_index=idx):
            rgb_ok = True
            break
    if not rgb_ok:
        cap.release()
        return None
    _live_cap = cap
    _live_cap_idx = idx
    return cap


def _generate_ffmpeg_mjpeg_stream(rgb_idx: int) -> Generator[bytes, None, None]:
    """Live MJPEG passthrough ffmpeg — niente decode/encode OpenCV (evita falsi IR)."""
    global _live_ffmpeg_proc, _resolved_rgb_idx, _live_rgb_diag
    fmt = _ffmpeg_input_format()
    size = (os.environ.get("D1_ORBBEC_FFMPEG_SIZE") or "640x480").strip()
    fps = max(2.0, min(15.0, float(os.environ.get("D1_ORBBEC_LIVE_FPS", "8"))))
    boundary = b"--frame\r\n"
    buf = b""
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "v4l2",
        "-input_format",
        fmt,
        "-video_size",
        size,
        "-framerate",
        str(int(fps)),
        "-i",
        f"/dev/video{int(rgb_idx)}",
        "-f",
        "mjpeg",
        "-q:v",
        "2",
        "pipe:1",
    ]
    with _cap_lock:
        _release_live_ffmpeg()
        try:
            _live_ffmpeg_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError:
            _live_ffmpeg_proc = None
            return
    proc = _live_ffmpeg_proc
    if proc is None or proc.stdout is None:
        return
    _resolved_rgb_idx = rgb_idx
    try:
        while not _live_stop.is_set():
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            while not _live_stop.is_set():
                start = buf.find(b"\xff\xd8")
                if start < 0:
                    buf = b""
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end < 0:
                    buf = buf[start:]
                    break
                jpeg = buf[start : end + 2]
                buf = buf[end + 2 :]
                if _rgb_only() and not _jpeg_passes_rgb_gate(jpeg, v4l_index=rgb_idx):
                    continue
                metrics = _jpeg_decode_metrics(jpeg)
                _live_rgb_diag = {
                    "index": rgb_idx,
                    "chroma": round(float(metrics[0]), 3) if metrics else None,
                    "rgb_like": bool(metrics and _index_passes_rgb_gate(rgb_idx, metrics[1], metrics[0])),
                }
                yield boundary + b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
    finally:
        with _cap_lock:
            _release_live_ffmpeg()


def generate_rgb_mjpeg_stream() -> Generator[bytes, None, None]:
    """MJPEG live — solo nodo UVC RGB (index 2), frame IR scartati sul JPEG servito."""
    if platform.system().lower() != "linux":
        return

    _live_stop.clear()
    rgb_idx = _live_stream_v4l_index()
    if rgb_idx is None:
        return
    if _use_ffmpeg_pipeline():
        yield from _generate_ffmpeg_mjpeg_stream(rgb_idx)
        return

    try:
        import cv2
    except ImportError:
        return

    fps = max(2.0, min(15.0, float(os.environ.get("D1_ORBBEC_LIVE_FPS", "8"))))
    period = 1.0 / fps
    boundary = b"--frame\r\n"
    bad_frames = 0
    global _resolved_rgb_idx, _live_rgb_diag
    _resolved_rgb_idx = rgb_idx
    spread_min = _min_channel_spread_for_index(rgb_idx)
    try:
        while not _live_stop.is_set():
            jpeg: bytes | None = None
            with _cap_lock:
                if _live_stop.is_set():
                    break
                cap = _open_live_cap(cv2, rgb_idx)
                if cap is None:
                    bad_frames += 1
                    time.sleep(period)
                    continue
                best_frame = None
                for _ in range(10):
                    ok, fr = cap.read()
                    if not ok or fr is None or not getattr(fr, "size", 0):
                        continue
                    if _rgb_only() and not _frame_looks_like_orbbec_rgb(
                        fr, min_spread=spread_min, v4l_index=rgb_idx
                    ):
                        continue
                    best_frame = fr
                    bad_frames = 0
                    break
                if best_frame is not None:
                    diag = _orbbec_frame_diagnostics(best_frame, v4l_index=rgb_idx)
                    _live_rgb_diag = {
                        "index": rgb_idx,
                        "chroma": diag.get("color_chroma"),
                        "rgb_like": bool(diag.get("rgb_like")),
                    }
                    jpeg = _encode_jpeg(best_frame, v4l_index=rgb_idx)
                else:
                    bad_frames += 1
            if _live_stop.is_set():
                break
            if jpeg:
                yield boundary + b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
            time.sleep(period)
    finally:
        with _cap_lock:
            _release_live_cap()

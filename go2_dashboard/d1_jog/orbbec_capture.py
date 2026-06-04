"""Cattura / stream Orbbec RGB (V4L diretto o HTTP operator :5052)."""

from __future__ import annotations

import os
import platform
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from pathlib import Path
from typing import Any

from go2_dashboard.cameras import (
    _USB_IDS_LOGICAL_0_ORBBEC,
    _enumerate_v4l_usb_bindings,
    _frame_channel_chroma_bgr,
    _frame_looks_like_rgb_color,
    _frame_rgb_diagnostics,
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


def prepare_camera_for_snapshot() -> None:
    """Ferma il live MJPEG e attende che il nodo V4L sia libero."""
    _live_stop.set()
    settle = float(os.environ.get("D1_ORBBEC_SNAPSHOT_SETTLE_S", "0.35"))
    if settle > 0:
        time.sleep(settle)


def _operator_base() -> str:
    return (
        os.environ.get("D1_OPERATOR_URL")
        or os.environ.get("HERMES_OPERATOR_URL")
        or "http://127.0.0.1:5052"
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


def orbbec_usb_v4l_indices() -> list[int]:
    """Tutti i nodi V4L collegati a Orbbec Gemini (2bc5:080b)."""
    rows = _enumerate_v4l_usb_bindings()
    return sorted({idx for idx, vid, pid in rows if (vid, pid) in _USB_IDS_LOGICAL_0_ORBBEC})


def _fixed_rgb_v4l_index() -> int | None:
    """Indice V4L RGB forzato (es. 6 sulla Gemini 335Lg) — niente auto su video4/IR."""
    raw = (os.environ.get("D1_ORBBEC_RGB_V4L_INDEX") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


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


def _frame_looks_like_orbbec_rgb(frame: Any) -> bool:
    if not _frame_looks_like_rgb_color(frame):
        return False
    return _frame_channel_chroma_bgr(frame) >= _orbbec_min_frame_chroma()


def _v4l_indices_probe_order() -> list[int]:
    """Solo nodi RGB candidati — mai tutti i /dev/video Orbbec (evita video2 IR)."""
    fixed = _fixed_rgb_v4l_index()
    if fixed is not None:
        return [fixed]
    manual = (os.environ.get("D1_ORBBEC_V4L_INDICES") or "").strip()
    preferred = _parse_index_list(
        manual or os.environ.get("D1_ORBBEC_RGB_V4L_PREFERRED", "6,4"),
    )
    if not preferred:
        preferred = [6, 4]
    all_orbbec = set(orbbec_usb_v4l_indices())
    order: list[int] = []
    for idx in preferred:
        if all_orbbec and idx not in all_orbbec:
            continue
        name = _v4l_sysfs_card_name(idx)
        if _v4l_sysfs_name_is_ir(name):
            continue
        if idx not in order:
            order.append(idx)
    return order or [i for i in preferred if i not in order] or [4, 6]


def _frame_score(frame: Any, *, sysfs_name: str = "") -> tuple[float, bool]:
    rgb = _frame_looks_like_orbbec_rgb(frame)
    chroma = _frame_channel_chroma_bgr(frame)
    bright = float(frame.max()) if frame is not None and getattr(frame, "size", 0) else 0.0
    score = (
        _v4l_sysfs_name_rgb_bonus(sysfs_name)
        + (3000.0 if rgb else 0.0)
        + chroma * 15.0
        + bright * 0.12
    )
    return score, rgb


def resolve_orbbec_rgb_v4l_index(*, force_probe: bool = False) -> int | None:
    """Sceglie /dev/videoN con vero colore BGR (esclude IR mono)."""
    global _resolved_rgb_idx
    fixed = _fixed_rgb_v4l_index()
    if fixed is not None and not force_probe:
        _resolved_rgb_idx = fixed
        return fixed
    if _resolved_rgb_idx is not None and not force_probe:
        return _resolved_rgb_idx
    if platform.system().lower() != "linux":
        return None
    try:
        import cv2
    except ImportError:
        return None

    best_idx: int | None = None
    best_score = -1.0
    best_rgb = False
    best_chroma = -1.0

    for idx in _v4l_indices_probe_order():
        sysfs_name = _v4l_sysfs_card_name(idx)
        if _v4l_sysfs_name_is_ir(sysfs_name):
            continue
        cap = _open_v4l_cap(cv2, idx)
        if cap is None:
            continue
        try:
            local_best = -1.0
            local_rgb = False
            local_frame = None
            for _ in range(14):
                ok, fr = cap.read()
                if not ok or fr is None or not getattr(fr, "size", 0):
                    continue
                sc, rgb = _frame_score(fr, sysfs_name=sysfs_name)
                if sc > local_best:
                    local_best = sc
                    local_rgb = rgb
                    local_frame = fr
            if local_frame is None:
                continue
            if _rgb_only() and not local_rgb:
                continue
            local_chroma = _frame_channel_chroma_bgr(local_frame)
            if local_best > best_score or (
                abs(local_best - best_score) < 1.0 and local_chroma > best_chroma
            ):
                best_score = local_best
                best_idx = idx
                best_rgb = local_rgb
                best_chroma = local_chroma
        finally:
            cap.release()

    if best_idx is not None and (best_rgb or not _rgb_only()):
        _resolved_rgb_idx = int(best_idx)
        return best_idx
    _resolved_rgb_idx = None
    return None


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

    if _v4l_sysfs_name_is_ir(_v4l_sysfs_card_name(use_idx)):
        return None, use_idx

    cap = _open_v4l_cap(cv2, use_idx)
    if cap is None:
        return None, use_idx
    sysfs_name = _v4l_sysfs_card_name(use_idx)
    try:
        best_frame = None
        best_score = -1.0
        best_rgb = False
        for _ in range(16):
            ok, fr = cap.read()
            if not ok or fr is None or not getattr(fr, "size", 0):
                continue
            sc, rgb = _frame_score(fr, sysfs_name=sysfs_name)
            if _rgb_only() and not rgb:
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


def _encode_jpeg(frame: Any) -> bytes | None:
    try:
        import cv2
    except ImportError:
        return None
    quality = int(os.environ.get("D1_ORBBEC_JPEG_QUALITY", "88"))
    ok_enc, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok_enc or buf is None or len(buf) < 400:
        return None
    return buf.tobytes()


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


def _candidate_http_urls() -> list[str]:
    """Solo preview V4L RGB espliciti — mai camera/0 (spesso IR su operator)."""
    base = _operator_base()
    urls: list[str] = []
    for idx in _v4l_indices_probe_order():
        urls.append(f"{base}/api/cameras/v4l/{idx}/preview.jpg")
    return urls


def _capture_v4l_direct_jpeg() -> tuple[bytes, str, dict[str, Any]] | None:
    global _resolved_rgb_idx
    with _cap_lock:
        for try_idx in _v4l_indices_probe_order():
            frame, idx = _read_rgb_frame(idx=try_idx)
            if frame is None or idx is None:
                continue
            diag = _frame_rgb_diagnostics(frame)
            kind = str(diag.get("stream_kind") or "")
            if _rgb_only() and (not diag.get("rgb_like") or kind == "mono_or_ir"):
                _resolved_rgb_idx = None
                continue
            jpeg = _encode_jpeg(frame)
            if jpeg is None:
                continue
            chroma = round(_frame_channel_chroma_bgr(frame), 2)
            sysfs_name = _v4l_sysfs_card_name(idx) if idx is not None else ""
            meta = {
                "v4l_index": idx,
                "v4l_sysfs_name": sysfs_name,
                "color_chroma": chroma,
                "stream_kind": "rgb",
            }
            src = f"v4l_rgb:/dev/video{idx} ({sysfs_name or 'no-name'} chroma={chroma})"
            _resolved_rgb_idx = idx
            return jpeg, src, meta
        return None


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
        if not operator_up:
            extra["note"] = "operator_5052_off_stream_rgb_v4l"
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

    # Immagine Teach: solo V4L RGB diretto (mai HTTP operator / camera IR).
    return _from_v4l()


def capture_orbbec_jpeg() -> dict[str, Any]:
    """Un frame Orbbec RGB (operator HTTP se up, altrimenti V4L diretto)."""
    global _resolved_rgb_idx
    if os.environ.get("D1_ORBBEC_REPROBE_EACH_CAPTURE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    ):
        _resolved_rgb_idx = None
    prepare_camera_for_snapshot()
    _SNAP_DIR.mkdir(parents=True, exist_ok=True)
    tried: list[str] = []
    operator_up = _operator_reachable()
    retries = max(1, int(os.environ.get("D1_ORBBEC_CAPTURE_RETRIES", "3")))
    allow_direct = os.environ.get("D1_ORBBEC_V4L_DIRECT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    for attempt in range(retries):
        if attempt > 0:
            time.sleep(float(os.environ.get("D1_ORBBEC_CAPTURE_RETRY_DELAY_S", "0.4")))
            _resolved_rgb_idx = None
        got = _try_capture_once(tried=tried, operator_up=operator_up)
        if got is not None and got.get("ok"):
            if attempt > 0:
                got["capture_attempt"] = attempt + 1
            return got

    hint_parts = [
        "Nessun frame RGB Orbbec — su NX i nodi colore sono spesso /dev/video4 e /dev/video6 "
        "(IR su video2). Imposta D1_ORBBEC_V4L_INDICES=4,6 se serve.",
    ]
    if not operator_up:
        hint_parts.append("Operator :5052 spenta (normale con D1 jog); uso solo V4L diretto.")
    if not allow_direct:
        hint_parts.append("Abilita D1_ORBBEC_V4L_DIRECT=1.")

    return {
        "ok": False,
        "reason": "orbbec_capture_failed",
        "operator": _operator_base(),
        "operator_reachable": operator_up,
        "rgb_only": _rgb_only(),
        "tried": tried,
        "hint": " ".join(hint_parts),
    }


def latest_snapshot_path() -> Path | None:
    p = _SNAP_DIR / _LATEST_NAME
    return p if p.is_file() else None


def generate_rgb_mjpeg_stream() -> Generator[bytes, None, None]:
    """MJPEG live da V4L RGB — un frame per volta sotto lock (non blocca snapshot)."""
    if platform.system().lower() != "linux":
        return
    try:
        import cv2  # noqa: F401
    except ImportError:
        return

    rgb_idx = resolve_orbbec_rgb_v4l_index()
    if rgb_idx is None:
        return

    _live_stop.clear()
    fps = max(2.0, min(15.0, float(os.environ.get("D1_ORBBEC_LIVE_FPS", "8"))))
    period = 1.0 / fps
    boundary = b"--frame\r\n"
    while not _live_stop.is_set():
        jpeg: bytes | None = None
        with _cap_lock:
            if _live_stop.is_set():
                break
            frame, _idx = _read_rgb_frame(idx=rgb_idx)
            if frame is not None:
                diag = _frame_rgb_diagnostics(frame)
                if _rgb_only() and not diag.get("rgb_like"):
                    continue
                jpeg = _encode_jpeg(frame)
        if _live_stop.is_set():
            break
        if jpeg:
            yield boundary + b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
        time.sleep(period)

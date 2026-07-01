"""Camera device labels, V4L helpers, and threaded JPEG cache for the dashboard."""

from __future__ import annotations

import os
import platform
import threading
import time
from pathlib import Path
from typing import Any

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

CAMERA_DEVICES: dict[int, str] = {
    0: "Wrist RGB (Orbbec Gemini / Sonix UVC — auto-map USB)",
    6: "Intel RealSense D435i RGB stream",
}

_USB_IDS_LOGICAL_0_ORBBEC = {("2bc5", "080b")}
_USB_IDS_LOGICAL_0_SONIX = {("0735", "0269")}
_USB_IDS_LOGICAL_0 = _USB_IDS_LOGICAL_0_ORBBEC | _USB_IDS_LOGICAL_0_SONIX
_USB_IDS_REALSENSE = {("8086", "0b3a")}


def _realsense_color_use_pyrs() -> bool:
    try:
        from go2_dashboard.realsense_pyrs import _backend_enabled

        return _backend_enabled()
    except Exception:
        return False


def _open_realsense_v4l_cap(v4l_index: int) -> Any:
    """Apre /dev/videoN RealSense provando fourcc (RGB di solito su video4)."""
    if cv2 is None:
        raise RuntimeError("cv2 unavailable")
    fourcc_order = os.environ.get("GO2_REALSENSE_V4L_FOURCC", "YUYV,MJPG,").split(",")
    for fc in fourcc_order:
        fc = fc.strip().upper()
        for opener in (
            lambda: cv2.VideoCapture(_v4l_path(v4l_index), cv2.CAP_V4L2),
            lambda: cv2.VideoCapture(int(v4l_index), cv2.CAP_V4L2),
        ):
            cap = opener()
            if not cap.isOpened():
                cap.release()
                continue
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            if fc and len(fc) >= 4:
                try:
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fc[:4]))
                except Exception:
                    pass
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            except Exception:
                pass
            ok_streak = 0
            for _ in range(8):
                ok, fr = cap.read()
                if ok and fr is not None and getattr(fr, "size", 0):
                    if _frame_looks_like_rgb_color(fr) or float(fr.max()) > 20:
                        ok_streak += 1
                        if ok_streak >= 2:
                            return cap
            cap.release()
    return _cv_videocapture(v4l_index)

# Cache: logico dashboard → indice /dev/videoN (solo Linux, sysfs USB).
_usb_auto_v4l_cache: dict[int, int] | None = None
_usb_auto_lock = threading.Lock()


def _usb_vid_pid_for_video_index(v4l_index: int) -> tuple[str, str] | None:
    """Legge idVendor/idProduct del device USB collegato a ``/sys/class/video4linux/videoN``."""
    if platform.system().lower() != "linux":
        return None
    dev = Path(f"/sys/class/video4linux/video{int(v4l_index)}/device")
    try:
        cur = dev.resolve()
    except OSError:
        return None
    for _ in range(20):
        vfile = cur / "idVendor"
        pfile = cur / "idProduct"
        if vfile.is_file() and pfile.is_file():
            try:
                return (vfile.read_text().strip().lower(), pfile.read_text().strip().lower())
            except OSError:
                return None
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return None


def _enumerate_v4l_usb_bindings() -> list[tuple[int, str, str]]:
    """Coppie (indice_video, vid, pid) per ogni nodo video4linux con USB dietro."""
    if platform.system().lower() != "linux":
        return []
    base = Path("/sys/class/video4linux")
    if not base.is_dir():
        return []
    out: list[tuple[int, str, str]] = []
    for name in sorted(os.listdir(base)):
        if not name.startswith("video"):
            continue
        tail = name[5:]
        if not tail.isdigit():
            continue
        idx = int(tail)
        pair = _usb_vid_pid_for_video_index(idx)
        if pair:
            out.append((idx, pair[0], pair[1]))
    return out


def _v4l_sysfs_card_name(v4l_index: int) -> str:
    """Nome interfaccia V4L (es. … Depth / IR / RGB) da sysfs — solo Linux."""
    if platform.system().lower() != "linux":
        return ""
    try:
        return Path(f"/sys/class/video4linux/video{int(v4l_index)}/name").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _realsense_rgb_node_priority(name: str) -> tuple[int, str]:
    """Ordina i nodi RealSense: RGB/color prima; depth/IR/metadata dopo.

    Ritorna (rank, name_lower) con rank crescente = più adatto allo stream colore AprilTag.
    """
    n = name.lower()
    if "meta" in n:
        return (80, n)
    if "rgb" in n or "color" in n:
        return (0, n)
    if "depth" in n:
        return (60, n)
    if "ir" in n or "infra" in n:
        return (50, n)
    # Molti kernel ripetono una stringa generica per tutti i nodi: serve anche il chroma-check sul frame.
    return (40, n)


def _frame_channel_chroma_bgr(frame: Any) -> float:
    """Differenza media tra canali BGR; ~0 su IR/grayscale replicato su 3 piani."""
    if cv2 is None or frame is None or len(frame.shape) != 3 or int(frame.shape[2]) < 3:
        return 0.0
    try:
        d0 = cv2.absdiff(frame[:, :, 0], frame[:, :, 1])
        d1 = cv2.absdiff(frame[:, :, 1], frame[:, :, 2])
        return float(cv2.mean(d0)[0] + cv2.mean(d1)[0])
    except Exception:
        return 0.0


def _frame_looks_like_rgb_color(frame: Any) -> bool:
    """Esclude nodi RealSense metadata / IR mono / grayscale-in-BGR: serve vero colore per AprilTag."""
    try:
        if frame is None or not getattr(frame, "size", 0):
            return False
        if len(frame.shape) != 3 or int(frame.shape[2]) < 3:
            return False
        if float(frame.max()) < 10.0:
            return False
        chroma_min = float(os.environ.get("GO2_REALSENSE_MIN_FRAME_CHROMA", "2.5"))
        return _frame_channel_chroma_bgr(frame) >= chroma_min
    except Exception:
        return False


def _frame_rgb_diagnostics(frame: Any) -> dict[str, Any]:
    """Sintesi leggera del frame per capire se è RGB vero o più simile a IR/grayscale."""
    chroma = _frame_channel_chroma_bgr(frame)
    rgb_like = _frame_looks_like_rgb_color(frame)
    if frame is None or not getattr(frame, "size", 0):
        return {"color_chroma": None, "rgb_like": False}
    return {
        "color_chroma": round(float(chroma), 3),
        "rgb_like": bool(rgb_like),
        "stream_kind": "rgb" if rgb_like else ("mono_or_ir" if chroma >= 0.0 else "unknown"),
    }


def _try_set_uvc_mjpeg_fourcc(cap: Any, *, prefer_env: str = "GO2_REALSENSE_PREFER_MJPEG") -> None:
    """Preferisci MJPEG su stream colore UVC (RealSense, Orbbec Gemini UVC, ecc.)."""
    if cv2 is None:
        return
    flag = os.environ.get(prefer_env, "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return
    try:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    except Exception:
        pass


def _try_set_realsense_mjpeg_fourcc(cap: Any) -> None:
    """Compat: stesso comportamento di ``_try_set_uvc_mjpeg_fourcc`` con env RealSense."""
    _try_set_uvc_mjpeg_fourcc(cap, prefer_env="GO2_REALSENSE_PREFER_MJPEG")


def _probe_generic_rgb_v4l(indices: list[int], *, default_env: str, fallback_default: int) -> int | None:
    """Prova nodi V4L finché uno restituisce frame BGR plausibili (non neri / non mono replicato)."""
    if cv2 is None or platform.system().lower() != "linux" or not indices:
        return None
    try:
        pref = int(os.environ.get(default_env, str(fallback_default)).strip())
    except ValueError:
        pref = int(fallback_default)
    order = sorted({int(idx) for idx in indices}, key=lambda idx: (abs(int(idx) - pref), int(idx)))
    for idx in order:
        cap = None
        try:
            path = _v4l_path(idx)
            cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
            if not cap.isOpened():
                if cap is not None:
                    cap.release()
                cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
            if cap is None or not cap.isOpened():
                continue
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            except Exception:
                pass
            ok_streak = 0
            for _ in range(14):
                ok, fr = cap.read()
                if ok and _frame_looks_like_rgb_color(fr):
                    ok_streak += 1
                    if ok_streak >= 2:
                        return int(idx)
                else:
                    ok_streak = 0
        except Exception:
            pass
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
    return None


def _score_realsense_v4l_nodes(rs_indices: list[int]) -> list[tuple[float, int]]:
    """Ordina nodi RealSense: (score, idx) — score più alto = frame più utile (luminosità + chroma)."""
    if cv2 is None or platform.system().lower() != "linux" or not rs_indices:
        return []
    scored: list[tuple[float, int]] = []
    for idx in sorted({int(i) for i in rs_indices}):
        cap = None
        try:
            path = _v4l_path(idx)
            cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
            if not cap.isOpened():
                if cap is not None:
                    cap.release()
                cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
            if cap is None or not cap.isOpened():
                continue
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            _try_set_uvc_mjpeg_fourcc(cap, prefer_env="GO2_REALSENSE_PREFER_MJPEG")
            rank, _ = _realsense_rgb_node_priority(_v4l_sysfs_card_name(idx))
            best = 0.0
            for _ in range(12):
                ok, fr = cap.read()
                if not ok or fr is None or not getattr(fr, "size", 0):
                    continue
                chroma = _frame_channel_chroma_bgr(fr)
                bright = float(fr.max())
                score = chroma * 4.0 + bright * 0.25 - float(rank)
                if _frame_looks_like_rgb_color(fr):
                    score += 50.0
                best = max(best, score)
            if best > 1.0:
                scored.append((best, int(idx)))
        except Exception:
            pass
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored


def _probe_realsense_rgb_v4l(rs_indices: list[int]) -> int | None:
    """Prova i nodi Intel (8086:0b3a) finché uno restituisce frame BGR non neri (spesso ≠ ``video6``)."""
    if cv2 is None or platform.system().lower() != "linux" or not rs_indices:
        return None
    flag = os.environ.get("GO2_REALSENSE_VIDEO_PROBE", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return None
    pref_s = os.environ.get("GO2_REALSENSE_V4L_DEFAULT", "4").strip()
    try:
        pref = int(pref_s)
    except ValueError:
        pref = 4
    scored: list[tuple[int, int, int]] = []
    for idx in rs_indices:
        rank, _ = _realsense_rgb_node_priority(_v4l_sysfs_card_name(idx))
        scored.append((rank, abs(int(idx) - int(pref)), int(idx)))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    order = [t[2] for t in scored]
    for idx in order:
        cap = None
        try:
            path = _v4l_path(idx)
            cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
            if not cap.isOpened():
                if cap is not None:
                    cap.release()
                cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
            if cap is None or not cap.isOpened():
                continue
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            _try_set_uvc_mjpeg_fourcc(cap, prefer_env="GO2_REALSENSE_PREFER_MJPEG")
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            except Exception:
                pass
            ok_streak = 0
            for _ in range(14):
                ok, fr = cap.read()
                if ok and _frame_looks_like_rgb_color(fr):
                    ok_streak += 1
                    if ok_streak >= 2:
                        return int(idx)
                else:
                    ok_streak = 0
        except Exception:
            pass
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
    return None


def usb_auto_v4l_mapping() -> dict[int, int]:
    """Mappa ``CAMERA_DEVICES`` logici → indice V4L reale da VID:PID (senza sovrascrivere ``GO2_VIDEO_INDEX_*``)."""
    global _usb_auto_v4l_cache
    with _usb_auto_lock:
        if _usb_auto_v4l_cache is not None:
            return dict(_usb_auto_v4l_cache)
        if platform.system().lower() != "linux":
            _usb_auto_v4l_cache = {}
            return {}
        rows = _enumerate_v4l_usb_bindings()
        m: dict[int, int] = {}
        logical0_idx = sorted(
            {
                idx
                for idx, vid, pid in rows
                if (vid, pid) in _USB_IDS_LOGICAL_0_ORBBEC
                or (vid, pid) in _USB_IDS_LOGICAL_0_SONIX
            }
        )
        if logical0_idx:
            try:
                pref0 = int(os.environ.get("GO2_ARM_CAMERA_V4L_DEFAULT", "0").strip())
            except ValueError:
                pref0 = 0
            logical0_orbbec = [
                idx for idx, vid, pid in rows if (vid, pid) in _USB_IDS_LOGICAL_0_ORBBEC
            ]
            logical0_sonix = [
                idx for idx, vid, pid in rows if (vid, pid) in _USB_IDS_LOGICAL_0_SONIX
            ]
            logical0_candidates = sorted({int(idx) for idx in (logical0_orbbec or logical0_sonix)})
            probed0 = _probe_generic_rgb_v4l(
                logical0_candidates,
                default_env="GO2_ARM_CAMERA_V4L_DEFAULT",
                fallback_default=pref0,
            )
            if probed0 is not None:
                m[0] = int(probed0)
            else:
                m[0] = int(min(logical0_candidates, key=lambda x: abs(int(x) - pref0)))
        rs_idx = sorted({idx for idx, vid, pid in rows if (vid, pid) in _USB_IDS_REALSENSE})
        if rs_idx:
            probed = _probe_realsense_rgb_v4l(rs_idx)
            if probed is not None:
                m[6] = int(probed)
            else:
                # Fallback: nodo apribile con frame più luminoso (evita IR nero su video5)
                scored_fb = _score_realsense_v4l_nodes(rs_idx)
                if scored_fb:
                    m[6] = int(scored_fb[0][1])
                else:
                    pref_s = os.environ.get("GO2_REALSENSE_V4L_DEFAULT", "4").strip()
                    try:
                        pref = int(pref_s)
                    except ValueError:
                        pref = 4
                    if pref in rs_idx:
                        m[6] = int(pref)
                    else:
                        m[6] = int(min(rs_idx, key=lambda x: abs(x - pref)))
        _usb_auto_v4l_cache = dict(m)
        return dict(_usb_auto_v4l_cache)


def _v4l_index_for_logical_camera(logical: int) -> int:
    """
    Indice V4L2 reale (``/dev/videoN``).

    Ordine di precedenza:

    1. ``GO2_VIDEO_INDEX_<logical>`` se impostato.
    2. Su Linux, mappa automatica USB (``GO2_CAMERA_AUTO_USB_MAP`` non ``0``/``false``):
       Sonix 0735:0269 / Orbbec 2bc5:080b → logico ``0``; Intel RealSense 8086:0b3a → logico ``6``
       (indice RGB scelto con probe frame BGR se ``GO2_REALSENSE_VIDEO_PROBE`` non disattivo,
       altrimenti ``GO2_REALSENSE_V4L_DEFAULT``, default ``6``). Per la camera logica ``0``
       il probe usa ``GO2_ARM_CAMERA_V4L_DEFAULT`` (default ``0``).
    3. Altrimenti ``logical == N`` → ``/dev/videoN``.
    """
    key = f"GO2_VIDEO_INDEX_{logical}"
    if key in os.environ:
        try:
            return int(str(os.environ[key]).strip())
        except ValueError:
            pass
    if platform.system().lower() == "linux":
        flag = os.environ.get("GO2_CAMERA_AUTO_USB_MAP", "1").strip().lower()
        if flag not in ("0", "false", "no", "off"):
            auto_m = usb_auto_v4l_mapping()
            if logical in auto_m:
                return int(auto_m[logical])
    return int(logical)


def _v4l_path(v4l_index: int) -> str:
    return f"/dev/video{int(v4l_index)}"


def _v4l_permission_hint(v4l_index: int) -> str | None:
    """Se il device esiste ma l'utente non può leggerlo (tipico RealSense su root:video)."""
    if platform.system().lower() != "linux":
        return None
    path = _v4l_path(v4l_index)
    try:
        if os.path.exists(path) and not os.access(path, os.R_OK):
            return (
                f"permesso negato su {path} (tipico Intel RealSense: gruppo video, MODE restrittivo). "
                "Sulla NX: installa la regola udev del repo (deploy_dashboard_to_nx) oppure "
                "`sudo usermod -aG video $USER` e riavvia la dashboard dopo nuovo login."
            )
    except OSError:
        return None
    return None


def _cv_videocapture(v4l_index: int) -> Any:
    """Apertura V4L2 su Linux: path ``/dev/videoN`` e indice numerico (RealSense/driver variabili)."""
    if cv2 is None:
        raise RuntimeError("cv2 unavailable")
    if platform.system().lower() == "linux":
        path = _v4l_path(v4l_index)
        try:
            cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
            if cap.isOpened():
                return cap
            cap.release()
        except Exception:
            pass
        try:
            cap = cv2.VideoCapture(v4l_index, cv2.CAP_V4L2)
            if cap.isOpened():
                return cap
            cap.release()
        except Exception:
            pass
    return cv2.VideoCapture(v4l_index)


class CameraCache:
    def __init__(self, devices: dict[int, str], fps: float = 20.0, jpeg_quality: int = 68):
        self.devices = devices
        self.period = 1.0 / max(fps, 1.0)
        self.jpeg_quality = jpeg_quality
        self.frames: dict[int, dict[str, Any]] = {}
        self.errors: dict[int, str] = {}
        self._stop = threading.Event()
        self._started_devices: set[int] = set()
        self._lock = threading.Lock()

    def start(self, device: int | None = None) -> None:
        if cv2 is None:
            return
        devices = [device] if device is not None else list(self.devices)
        for dev in devices:
            if dev not in self.devices or dev in self._started_devices:
                continue
            self._started_devices.add(dev)
            threading.Thread(target=self._loop, args=(dev,), daemon=True).start()

    def _loop_pyrs(self, device: int) -> None:
        """Stream colore RealSense via pyrealsense2 (RGB, non IR /dev/video2)."""
        from go2_dashboard import realsense_pyrs as rp

        warmup_left = int(os.environ.get("GO2_REALSENSE_WARMUP_FRAMES", "6"))
        while not self._stop.is_set():
            if not rp.start():
                st = rp.status()
                with self._lock:
                    self.errors[device] = st.get("error") or (
                        "pyrealsense2: camera occupata — ferma dashboard 5052 "
                        "(nx_dashboard_supervise) e riavvia: bash scripts/nx_start_d1_jog.sh"
                    )
                time.sleep(3.0)
                continue
            if warmup_left > 0:
                rp.warmup(warmup_left)
                warmup_left = 0
            start = time.perf_counter()
            bundle = rp.read_bundle()
            frame = bundle.color if bundle is not None else None
            if frame is None:
                with self._lock:
                    self.errors[device] = rp.status().get("error") or "pyrs no frame"
                rp.stop()
                time.sleep(1.0)
                continue
            enc_ok, jpg = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
            )
            if enc_ok:
                diag = _frame_rgb_diagnostics(frame)
                with self._lock:
                    self.frames[device] = {
                        "jpg": jpg.tobytes(),
                        "ts": time.time(),
                        "shape": list(frame.shape),
                        "label": self.devices[device],
                        "color_chroma": diag.get("color_chroma"),
                        "rgb_like": True,
                        "stream_kind": "rgb",
                        "capture_backend": "pyrealsense2",
                    }
                    self.errors.pop(device, None)
            delay = self.period - (time.perf_counter() - start)
            if delay > 0:
                time.sleep(delay)
        rp.stop()

    def _loop(self, device: int) -> None:
        if int(device) == 6 and _realsense_color_use_pyrs():
            self._loop_pyrs(device)
            return
        cap = None
        while not self._stop.is_set():
            start = time.perf_counter()
            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                v4l_idx = _v4l_index_for_logical_camera(device)
                if int(device) == 6:
                    cap = _open_realsense_v4l_cap(v4l_idx)
                else:
                    cap = _cv_videocapture(v4l_idx)
                if cap.isOpened():
                    try:
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    except Exception:
                        pass
                    usb = _usb_vid_pid_for_video_index(v4l_idx)
                    if int(device) == 6 and usb == ("8086", "0b3a"):
                        _try_set_uvc_mjpeg_fourcc(cap, prefer_env="GO2_REALSENSE_PREFER_MJPEG")
                    elif int(device) == 0 and usb == ("2bc5", "080b"):
                        _try_set_uvc_mjpeg_fourcc(cap, prefer_env="GO2_ORBBEC_PREFER_MJPEG")
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    cap.set(cv2.CAP_PROP_FPS, 15)
                else:
                    hint = _v4l_permission_hint(v4l_idx)
                    msg = f"open failed (V4L /dev/video{v4l_idx}, logical {device})"
                    if hint:
                        msg = f"{msg} — {hint}"
                    with self._lock:
                        self.errors[device] = msg
                    time.sleep(1.0)
                    continue

            ok, frame = (False, None)
            try:
                ok, frame = cap.read()
            except Exception as exc:
                with self._lock:
                    self.errors[device] = f"read failed: {exc!r}"
                cap.release()
                cap = None
                time.sleep(0.5)
                continue

            if ok and frame is not None:
                diag_pre = _frame_rgb_diagnostics(frame)
                if frame.size and float(frame.max()) < 4.0:
                    with self._lock:
                        self.errors[device] = (
                            f"frame nero su V4L — verifica GO2_VIDEO_INDEX_{device} "
                            f"(reale /dev/video{_v4l_index_for_logical_camera(device)}, logico {device})"
                        )
                    time.sleep(0.25)
                    continue
                if int(device) == 6 and not diag_pre.get("rgb_like"):
                    global _usb_auto_v4l_cache
                    with _usb_auto_lock:
                        _usb_auto_v4l_cache = None
                    cap.release()
                    cap = None
                    with self._lock:
                        self.errors[device] = (
                            f"stream non RGB su /dev/video{_v4l_index_for_logical_camera(device)} — "
                            "usa GO2_REALSENSE_COLOR_BACKEND=pyrs oppure GO2_VIDEO_INDEX_6=4"
                        )
                    time.sleep(0.8)
                    continue
                enc_ok, jpg = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
                )
                if enc_ok:
                    diag = _frame_rgb_diagnostics(frame)
                    with self._lock:
                        self.frames[device] = {
                            "jpg": jpg.tobytes(),
                            "ts": time.time(),
                            "shape": list(frame.shape),
                            "label": self.devices[device],
                            "color_chroma": diag.get("color_chroma"),
                            "rgb_like": bool(diag.get("rgb_like")),
                            "stream_kind": diag.get("stream_kind"),
                            "capture_backend": "v4l2",
                        }
                        self.errors.pop(device, None)
            else:
                with self._lock:
                    self.errors[device] = "read returned no frame"
                cap.release()
                cap = None
                time.sleep(0.5)
                continue
            delay = self.period - (time.perf_counter() - start)
            if delay > 0:
                time.sleep(delay)
        if cap is not None:
            cap.release()

    def get_jpeg(self, device: int, wait_s: float = 1.2) -> bytes | None:
        self.start(device)
        deadline = time.time() + wait_s
        while True:
            with self._lock:
                item = self.frames.get(device)
                if item is not None and time.time() - item["ts"] < 3.0:
                    return item["jpg"]
            if time.time() >= deadline:
                return None
            time.sleep(0.04)

    def peek_jpeg(self, device: int) -> bytes | None:
        """Ultimo frame in cache senza attesa (per MJPEG: niente blocchi lunghi sul generator)."""
        self.start(device)
        with self._lock:
            item = self.frames.get(device)
            if item is None:
                return None
            return item["jpg"]

    def stats(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            return {
                str(device): {
                    "label": self.devices[device],
                    "available": device in self.frames and (now - self.frames[device]["ts"]) < 5.0,
                    "started": device in self._started_devices,
                    "age_ms": None if device not in self.frames else round((now - self.frames[device]["ts"]) * 1000, 1),
                    "shape": None if device not in self.frames else self.frames[device]["shape"],
                    "color_chroma": None if device not in self.frames else self.frames[device].get("color_chroma"),
                    "rgb_like": False if device not in self.frames else bool(self.frames[device].get("rgb_like")),
                    "stream_kind": None if device not in self.frames else self.frames[device].get("stream_kind"),
                    "capture_backend": None if device not in self.frames else self.frames[device].get("capture_backend"),
                    "error": self.errors.get(device),
                }
                for device in self.devices
            }


CAMERA_CACHE = CameraCache(
    CAMERA_DEVICES,
    fps=float(os.environ.get("GO2_CAMERA_CACHE_FPS", "20")),
)

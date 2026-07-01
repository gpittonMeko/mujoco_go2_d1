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

from go2_dashboard import orbbec_lock

CAMERA_DEVICES: dict[int, str] = {
    0: "Wrist Intel RealSense D456 RGB stream",
    6: "Front Intel RealSense D435i RGB stream",
}

_USB_IDS_LOGICAL_0_ORBBEC = {("2bc5", "080b")}
_USB_IDS_LOGICAL_0_SONIX = {("0735", "0269")}
_USB_IDS_LOGICAL_0 = _USB_IDS_LOGICAL_0_ORBBEC | _USB_IDS_LOGICAL_0_SONIX
_USB_IDS_REALSENSE_D435 = {("8086", "0b3a")}
_USB_IDS_REALSENSE_D456 = {("8086", "0b5c")}
_USB_IDS_REALSENSE = _USB_IDS_REALSENSE_D435 | _USB_IDS_REALSENSE_D456


def wrist_depth_backend() -> str:
    """Backend depth metrica polso: ``realsense`` (D456, default deploy) o ``orbbec`` (lab legacy)."""
    b = (os.environ.get("GO2_WRIST_DEPTH_BACKEND") or "realsense").strip().lower()
    if b in ("orbbec", "pyorbbec", "ob", "pyorbbecsdk"):
        return "orbbec"
    return "realsense"


def mjpeg_stream_period_s() -> float:
    """Compat helper for older operator routes: current MJPEG frame period."""
    raw = (
        os.environ.get("GO2_MJPEG_FRAME_PERIOD_S")
        or os.environ.get("GO2_CAMERA_STREAM_PERIOD_S")
        or ""
    ).strip()
    if raw:
        try:
            return max(0.02, float(raw))
        except ValueError:
            pass
    fps_raw = (os.environ.get("GO2_CAMERA_CACHE_FPS") or "10").strip()
    try:
        fps = max(1.0, float(fps_raw))
    except ValueError:
        fps = 10.0
    return 1.0 / fps


def _norm_usb_pid(raw: str) -> tuple[str, str]:
    p = raw.strip().lower().replace("0x", "")
    return ("8086", p)


def _wrist_realsense_usb_ids() -> set[tuple[str, str]]:
    raw = (os.environ.get("GO2_WRIST_REALSENSE_USB_PID") or "0b5c").strip()
    return {_norm_usb_pid(raw)}


def _front_realsense_usb_ids() -> set[tuple[str, str]]:
    raw = (os.environ.get("GO2_FRONT_REALSENSE_USB_PID") or "0b3a").strip()
    return {_norm_usb_pid(raw)}

# Cache: logico dashboard → indice /dev/videoN (solo Linux, sysfs USB).
_usb_auto_v4l_cache: dict[int, int] | None = None
_usb_auto_lock = threading.Lock()

# Diagnostica ultima scelta V4L per log.0 Orbbec (``GET /api/cameras/status``).
_ORBBEC_LOGICAL0_DEBUG: dict[str, Any] = {}

# Override da UI operator (senza riavvio): ha priorità sulla mappa USB automatica, mai su ``GO2_VIDEO_INDEX_*``.
_runtime_v4l_lock = threading.Lock()
_runtime_v4l_by_logical: dict[int, int] = {}


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


def v4l_usb_inventory() -> list[dict[str, Any]]:
    """Tutti i nodi ``/dev/video*`` legati a USB: sysfs name + VID:PID + slot logici dashboard (0/6) se mappati.

    La dashboard HTTP espone **solo** i device logici ``0`` e ``6`` (JPEG/MJPEG). Gli altri nodi restano qui per debug
    (Orbbec/RealSense espongono spesso depth/IR/metadata su indici diversi dal colore). Per Orbbec, la mappa automatica
    prova prima i nodi il cui nome sysfs indica RGB, esclude depth/IR dal probe salvo
    ``GO2_ORBBEC_PROBE_INCLUDE_DEPTH_IR=1``, e può essere forzata con ``GO2_VIDEO_INDEX_0``.
    """
    rows = _enumerate_v4l_usb_bindings()
    auto_m = usb_auto_v4l_mapping()
    rev: dict[int, list[int]] = {}
    for log, vidx in auto_m.items():
        rev.setdefault(int(vidx), []).append(int(log))
    out: list[dict[str, Any]] = []
    for idx, vid, pid in sorted({(r[0], r[1], r[2]) for r in rows}, key=lambda t: t[0]):
        name = _v4l_sysfs_card_name(idx)
        slots = sorted(rev.get(int(idx), []))
        out.append(
            {
                "v4l_index": int(idx),
                "sysfs_name": name,
                "usb_vid_pid": f"{vid}:{pid}",
                "dashboard_logical_slots": slots,
            }
        )
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


def _orbbec_rgb_node_priority(name: str) -> tuple[int, str]:
    """Priorità nodi V4L Orbbec Gemini (UVC): stesso schema di RealSense.

    Il pattern IR / proiettore depth (``puntini``) esce spesso su nodi depth/IR — vanno provati dopo RGB.
    """
    n = (name or "").lower()
    if "meta" in n:
        return (85, n)
    if "rgb" in n or "color" in n or "colour" in n:
        return (0, n)
    if "depth" in n:
        return (65, n)
    if "ir" in n or "infra" in n:
        return (55, n)
    return (40, n)


def _orbbec_sysfs_name_is_non_color_stream(name: str) -> bool:
    """Vero se il nome sysfs indica depth/IR/mono (non va usato come ``CAMERA_DEVICES[0]`` senza probe ok)."""
    n = (name or "").lower()
    if "rgb" in n or "color" in n or "colour" in n:
        return False
    if "depth" in n or "ir" in n or "infra" in n or "mono" in n:
        return True
    return False


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


def _orbbec_probe_chroma_min() -> float:
    raw = (os.environ.get("GO2_ORBBEC_MIN_FRAME_CHROMA") or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    try:
        return float(os.environ.get("GO2_REALSENSE_MIN_FRAME_CHROMA", "2.5"))
    except ValueError:
        return 2.5


def _frame_looks_like_rgb_color_with_min_chroma(frame: Any, chroma_min: float) -> bool:
    """Come ``_frame_looks_like_rgb_color`` ma soglia chroma esplicita (Orbbec vs RealSense)."""
    try:
        if frame is None or not getattr(frame, "size", 0):
            return False
        if len(frame.shape) != 3 or int(frame.shape[2]) < 3:
            return False
        if float(frame.max()) < 10.0:
            return False
        return _frame_channel_chroma_bgr(frame) >= float(chroma_min)
    except Exception:
        return False


def _orbbec_max_edge_from_env(*, relaxed: bool) -> float:
    if relaxed:
        raw = (os.environ.get("GO2_ORBBEC_MAX_EDGE_DENSITY_RELAXED") or "0.42").strip()
    else:
        raw = (os.environ.get("GO2_ORBBEC_MAX_EDGE_DENSITY") or "0.26").strip()
    try:
        return float(raw or ("0.42" if relaxed else "0.26"))
    except ValueError:
        return 0.42 if relaxed else 0.26


def _frame_orbbec_color_plausible(
    frame: Any,
    chroma_min: float,
    *,
    max_edge_den: float | None = None,
) -> bool:
    """Esclude stream IR/proiettore depth: reticolo ad alta densità di bordi rispetto a RGB UVC."""
    if not _frame_looks_like_rgb_color_with_min_chroma(frame, chroma_min):
        return False
    if cv2 is None:
        return True
    if max_edge_den is None:
        max_edge_den = _orbbec_max_edge_from_env(relaxed=False)
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 45, 120)
        den = float(edges.mean()) / 255.0
        if den > float(max_edge_den):
            return False
    except Exception:
        return True
    return True


def orbbec_logical0_probe_debug() -> dict[str, Any]:
    """Ultima risoluzione probe Orbbec per log.0 (diagnostica ``GET /api/cameras/status``)."""
    return dict(_ORBBEC_LOGICAL0_DEBUG)


def _probe_orbbec_rgb_v4l(indices: list[int], *, default_env: str, fallback_default: int) -> int | None:
    """Sceglie il nodo colore Orbbec: più passaggi (MJPEG/YUYV, soglia bordi, anche sysfs ambigui)."""
    global _ORBBEC_LOGICAL0_DEBUG
    _ORBBEC_LOGICAL0_DEBUG = {"stage": "init"}
    if cv2 is None or platform.system().lower() != "linux" or not indices:
        _ORBBEC_LOGICAL0_DEBUG = {"stage": "no_cv2_or_empty_indices"}
        return None
    flag = os.environ.get("GO2_ORBBEC_VIDEO_PROBE", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        _ORBBEC_LOGICAL0_DEBUG = {"stage": "GO2_ORBBEC_VIDEO_PROBE_off"}
        return None
    try:
        pref = int(os.environ.get(default_env, str(fallback_default)).strip())
    except ValueError:
        pref = int(fallback_default)
    chroma_min = _orbbec_probe_chroma_min()
    allow_depth_ir = os.environ.get("GO2_ORBBEC_PROBE_INCLUDE_DEPTH_IR", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    uniq = sorted({int(idx) for idx in indices})

    def sort_key(idx: int, *, penalize_noncolor: bool) -> tuple[int, int, int, int]:
        name = _v4l_sysfs_card_name(idx)
        noncolor = _orbbec_sysfs_name_is_non_color_stream(name)
        rank, _ = _orbbec_rgb_node_priority(name)
        penalize = 1 if (penalize_noncolor and noncolor and not allow_depth_ir) else 0
        return (penalize, rank, abs(int(idx) - int(pref)), int(idx))

    def try_pass(
        *,
        pass_name: str,
        penalize_noncolor: bool,
        relaxed_edge: bool,
        use_mjpeg: bool,
        reads: int,
    ) -> int | None:
        order = sorted(uniq, key=lambda i: sort_key(i, penalize_noncolor=penalize_noncolor))
        max_edge = _orbbec_max_edge_from_env(relaxed=relaxed_edge)
        for idx in order:
            name = _v4l_sysfs_card_name(idx)
            if penalize_noncolor and _orbbec_sysfs_name_is_non_color_stream(name) and not allow_depth_ir:
                continue
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
                if use_mjpeg:
                    _try_set_uvc_mjpeg_fourcc(cap, prefer_env="GO2_ORBBEC_PREFER_MJPEG")
                else:
                    try:
                        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV"))
                    except Exception:
                        pass
                try:
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                except Exception:
                    pass
                ok_streak = 0
                for _ in range(reads):
                    ok, fr = cap.read()
                    if ok and _frame_orbbec_color_plausible(fr, chroma_min, max_edge_den=max_edge):
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

    # Il probe apre i nodi Orbbec 0-3 in lettura: serializza col lock per non rubare la
    # camera a una presa SDK / a un altro processo. Se resta occupato, salta il probe.
    try:
        probe_timeout = float((os.environ.get("GO2_ORBBEC_PROBE_LOCK_TIMEOUT_S") or "6").strip())
    except ValueError:
        probe_timeout = 6.0
    with orbbec_lock.orbbec_guard("orbbec_probe", blocking=True, timeout_s=probe_timeout) as _lk:
        if not _lk.acquired:
            _ORBBEC_LOGICAL0_DEBUG = {
                "ok": False,
                "stage": "orbbec_busy_skip_probe",
                "holder": _lk.holder,
                "pref": pref,
                "indices_pool": uniq,
            }
            return None
        for spec in (
            ("strict_sysfs_mjpeg", True, False, True, 18),
            ("relaxed_edge_mjpeg", True, True, True, 18),
            ("relaxed_edge_yuyv", True, True, False, 22),
            ("exhaustive_noncolor_mjpeg", False, True, True, 16),
            ("exhaustive_noncolor_yuyv", False, True, False, 20),
        ):
            pname, pnc, re, umj, nread = spec
            found = try_pass(
                pass_name=pname,
                penalize_noncolor=pnc,
                relaxed_edge=re,
                use_mjpeg=umj,
                reads=nread,
            )
            if found is not None:
                _ORBBEC_LOGICAL0_DEBUG = {
                    "ok": True,
                    "v4l_index": found,
                    "probe_pass": pname,
                    "pref": pref,
                    "indices_pool": uniq,
                }
                return found
    _ORBBEC_LOGICAL0_DEBUG = {
        "ok": False,
        "stage": "all_probe_passes_failed",
        "pref": pref,
        "indices_pool": uniq,
    }
    return None


def _orbbec_rgb_fallback_v4l_index(indices: list[int], pref: int) -> int:
    """Se il probe fallisce: preferisci sysfs ``rgb``/``color``, evita depth/IR se il nome lo dice."""
    if not indices:
        return int(pref)

    def sort_key(idx: int) -> tuple[int, int, int, int]:
        name = _v4l_sysfs_card_name(int(idx))
        noncolor = _orbbec_sysfs_name_is_non_color_stream(name)
        rank, _ = _orbbec_rgb_node_priority(name)
        return (1 if noncolor else 0, rank, abs(int(idx) - int(pref)), int(idx))

    return int(min(indices, key=sort_key))


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


def _probe_realsense_rgb_v4l(rs_indices: list[int]) -> int | None:
    """Prova i nodi Intel (8086:0b3a) finché uno restituisce frame BGR non neri (spesso ≠ ``video6``)."""
    if cv2 is None or platform.system().lower() != "linux" or not rs_indices:
        return None
    flag = os.environ.get("GO2_REALSENSE_VIDEO_PROBE", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return None
    pref_s = os.environ.get("GO2_REALSENSE_V4L_DEFAULT", "6").strip()
    try:
        pref = int(pref_s)
    except ValueError:
        pref = 6
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
        if wrist_depth_backend() != "realsense":
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
                if logical0_orbbec:
                    logical0_candidates = sorted({int(idx) for idx in logical0_orbbec})
                    probed0 = _probe_orbbec_rgb_v4l(
                        logical0_candidates,
                        default_env="GO2_ARM_CAMERA_V4L_DEFAULT",
                        fallback_default=pref0,
                    )
                    if probed0 is not None:
                        m[0] = int(probed0)
                    else:
                        fb = int(_orbbec_rgb_fallback_v4l_index(logical0_candidates, pref0))
                        m[0] = fb
                        _ORBBEC_LOGICAL0_DEBUG = {
                            "ok": False,
                            "method": "sysfs_fallback_after_failed_probe",
                            "v4l_index": fb,
                            "pref": pref0,
                            "hint_it": (
                                "Nessun frame ha superato il probe RGB Orbbec: "
                                "prova export GO2_VIDEO_INDEX_0=N sul nodo RGB da v4l2-ctl, "
                                "oppure aumenta GO2_ORBBEC_MAX_EDGE_DENSITY_RELAXED."
                            ),
                        }
                else:
                    logical0_candidates = sorted({int(idx) for idx in logical0_sonix})
                    probed0 = _probe_generic_rgb_v4l(
                        logical0_candidates,
                        default_env="GO2_ARM_CAMERA_V4L_DEFAULT",
                        fallback_default=pref0,
                    )
                    if probed0 is not None:
                        m[0] = int(probed0)
                    else:
                        m[0] = int(min(logical0_candidates, key=lambda x: abs(int(x) - pref0)))
        wrist_rs_idx = sorted(
            {idx for idx, vid, pid in rows if (vid, pid) in _wrist_realsense_usb_ids()}
        )
        front_rs_idx = sorted(
            {idx for idx, vid, pid in rows if (vid, pid) in _front_realsense_usb_ids()}
        )
        if wrist_depth_backend() == "realsense" and wrist_rs_idx:
            probed_w = _probe_realsense_rgb_v4l(wrist_rs_idx)
            if probed_w is not None:
                m[0] = int(probed_w)
            else:
                try:
                    pref_w = int(os.environ.get("GO2_WRIST_REALSENSE_V4L_DEFAULT", "0").strip())
                except ValueError:
                    pref_w = 0
                if pref_w in wrist_rs_idx:
                    m[0] = int(pref_w)
                else:
                    m[0] = int(min(wrist_rs_idx, key=lambda x: abs(x - pref_w)))
        if front_rs_idx:
            probed_f = _probe_realsense_rgb_v4l(front_rs_idx)
            if probed_f is not None:
                m[6] = int(probed_f)
            else:
                pref_s = os.environ.get("GO2_REALSENSE_V4L_DEFAULT", "6").strip()
                try:
                    pref = int(pref_s)
                except ValueError:
                    pref = 6
                if pref in front_rs_idx:
                    m[6] = int(pref)
                else:
                    m[6] = int(min(front_rs_idx, key=lambda x: abs(x - pref)))
        elif wrist_depth_backend() == "orbbec":
            rs_idx = sorted({idx for idx, vid, pid in rows if (vid, pid) in _USB_IDS_REALSENSE})
            if rs_idx:
                probed = _probe_realsense_rgb_v4l(rs_idx)
                if probed is not None:
                    m[6] = int(probed)
                else:
                    try:
                        pref = int(os.environ.get("GO2_REALSENSE_V4L_DEFAULT", "6").strip())
                    except ValueError:
                        pref = 6
                    if pref in rs_idx:
                        m[6] = int(pref)
                    else:
                        m[6] = int(min(rs_idx, key=lambda x: abs(x - pref)))
        _usb_auto_v4l_cache = dict(m)
        return dict(_usb_auto_v4l_cache)


def v4l_open_candidates_for_logical(logical: int) -> list[int]:
    """Indici V4L da provare in ordine se il mapping primario non apre (env errato, USB riordinato)."""
    seen: set[int] = set()
    out: list[int] = []

    def _add(idx: int) -> None:
        i = int(idx)
        if i not in seen:
            seen.add(i)
            out.append(i)

    _add(_v4l_index_for_logical_camera(logical))
    if int(logical) == 0:
        try:
            probe = orbbec_logical0_probe_debug()
            if probe.get("v4l_index") is not None:
                _add(int(probe["v4l_index"]))
        except Exception:
            pass
    try:
        auto = usb_auto_v4l_mapping()
        if int(logical) in auto:
            _add(int(auto[int(logical)]))
    except Exception:
        pass
    for idx in v4l_candidates_for_logical_slot(int(logical)):
        _add(int(idx))
    return out


def _v4l_index_for_logical_camera(logical: int) -> int:
    """
    Indice V4L2 reale (``/dev/videoN``).

    Ordine di precedenza:

    1. ``GO2_VIDEO_INDEX_<logical>`` se impostato.
    2. Su Linux, mappa automatica USB (``GO2_CAMERA_AUTO_USB_MAP`` non ``0``/``false``):
       Sonix 0735:0269 / Orbbec 2bc5:080b → logico ``0``; Intel RealSense 8086:0b3a → logico ``6``.
       RealSense: probe BGR se ``GO2_REALSENSE_VIDEO_PROBE`` è attivo, altrimenti
       ``GO2_REALSENSE_V4L_DEFAULT`` (default ``6``). Orbbec: probe sysfs (RGB prima) + chroma se
       ``GO2_ORBBEC_VIDEO_PROBE`` è attivo; preferenza indice ``GO2_ARM_CAMERA_V4L_DEFAULT`` (default ``0``).
    3. Altrimenti ``logical == N`` → ``/dev/videoN``.
    """
    key = f"GO2_VIDEO_INDEX_{logical}"
    if key in os.environ:
        try:
            env_idx = int(str(os.environ[key]).strip())
            if _v4l_sysfs_node_exists(env_idx):
                return env_idx
            # Env puntava a /dev/videoN inesistente (es. video6 dopo enumerazione USB parziale).
            _ORBBEC_LOGICAL0_DEBUG.update(
                {
                    "env_override_missing": env_idx,
                    "hint_it": f"{key}={env_idx} assente — fallback auto-map/probe",
                }
            )
        except ValueError:
            pass
    with _runtime_v4l_lock:
        rt = _runtime_v4l_by_logical.get(int(logical))
        if rt is not None:
            return int(rt)
    if platform.system().lower() == "linux":
        flag = os.environ.get("GO2_CAMERA_AUTO_USB_MAP", "1").strip().lower()
        if flag not in ("0", "false", "no", "off"):
            auto_m = usb_auto_v4l_mapping()
            if logical in auto_m:
                return int(auto_m[logical])
    return int(logical)


def _v4l_path(v4l_index: int) -> str:
    return f"/dev/video{int(v4l_index)}"


def _v4l_sysfs_node_exists(v4l_index: int) -> bool:
    """Vero se il nodo sysfs ``videoN`` esiste (evita env obsoleti tipo GO2_VIDEO_INDEX_0=6 assente)."""
    if platform.system().lower() != "linux":
        return True
    try:
        return os.path.isdir(f"/sys/class/video4linux/video{int(v4l_index)}")
    except (TypeError, ValueError, OSError):
        return False


_ORBBEC_USB_VENDOR = "2bc5"


def _v4l_is_orbbec(v4l_index: int) -> bool:
    """Vero se il nodo ``/dev/videoN`` è un Orbbec (vendor 2bc5): va serializzato col lock."""
    usb = _usb_vid_pid_for_video_index(int(v4l_index))
    if not usb:
        return False
    return usb in _USB_IDS_LOGICAL_0_ORBBEC or usb[0] == _ORBBEC_USB_VENDOR


def _logical_uses_orbbec(logical: int) -> bool:
    """Vero se lo slot logico aprirebbe un device Orbbec (almeno un candidato V4L è Orbbec)."""
    if int(logical) != 0 or wrist_depth_backend() != "orbbec":
        return False
    try:
        for idx in v4l_open_candidates_for_logical(int(logical)):
            if _v4l_is_orbbec(int(idx)):
                return True
    except Exception:
        return False
    return False


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


def debug_v4l_snapshot_jpeg(v4l_index: int, *, jpeg_quality: int = 72) -> bytes | None:
    """Un singolo frame JPEG da ``/dev/videoN`` (debug: confrontare nodi RGB vs depth IR senza SDK)."""
    if cv2 is None or platform.system().lower() != "linux":
        return None
    # Snapshot Orbbec: serializza col lock (non rubare la camera a una presa SDK / altro processo).
    _snap_lease = None
    if _v4l_is_orbbec(int(v4l_index)):
        try:
            _snap_to = float((os.environ.get("GO2_ORBBEC_SNAPSHOT_LOCK_TIMEOUT_S") or "4").strip())
        except ValueError:
            _snap_to = 4.0
        _snap_lease = orbbec_lock.acquire("debug_snapshot", blocking=True, timeout_s=_snap_to)
        if _snap_lease is None:
            return None
    cap = None
    try:
        cap = _cv_videocapture(int(v4l_index))
        if not cap.isOpened():
            return None
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        usb = _usb_vid_pid_for_video_index(int(v4l_index))
        if usb == ("2bc5", "080b"):
            _try_set_uvc_mjpeg_fourcc(cap, prefer_env="GO2_ORBBEC_PREFER_MJPEG")
        elif usb in _USB_IDS_REALSENSE:
            _try_set_uvc_mjpeg_fourcc(cap, prefer_env="GO2_REALSENSE_PREFER_MJPEG")
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        except Exception:
            pass
        for _ in range(10):
            ok, frame = cap.read()
            if ok and frame is not None and frame.size and float(frame.max()) >= 4.0:
                enc_ok, jpg = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)],
                )
                if enc_ok:
                    return jpg.tobytes()
        return None
    except Exception:
        return None
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        orbbec_lock.release(_snap_lease)


def v4l_index_in_usb_inventory(v4l_index: int) -> bool:
    """Vero se ``v4l_index`` compare nell'inventario USB (evita path traversal su indici casuali)."""
    want = int(v4l_index)
    return any(int(r[0]) == want for r in _enumerate_v4l_usb_bindings())


def _expected_usb_ids_for_logical(logical: int) -> set[tuple[str, str]]:
    if int(logical) == 0:
        if wrist_depth_backend() == "realsense":
            return set(_wrist_realsense_usb_ids())
        return set(_USB_IDS_LOGICAL_0)
    if int(logical) == 6:
        return set(_front_realsense_usb_ids())
    return set()


def v4l_candidates_for_logical_slot(logical: int) -> list[int]:
    """Tutti i ``/dev/videoN`` USB compatibili con lo slot dashboard (0 = polso, 6 = frontale)."""
    want = _expected_usb_ids_for_logical(int(logical))
    if not want:
        return []
    rows = _enumerate_v4l_usb_bindings()
    return sorted({int(idx) for idx, vid, pid in rows if (vid, pid) in want})


def validate_runtime_v4l_for_logical(logical: int, v4l_index: int) -> str | None:
    """Ritorna messaggio errore (IT) o ``None`` se l'indice è ammesso per lo slot."""
    if int(logical) not in (0, 6):
        return "logical deve essere 0 o 6"
    if not v4l_index_in_usb_inventory(v4l_index):
        return "indice V4L non nell'inventario USB"
    usb = _usb_vid_pid_for_video_index(int(v4l_index))
    if not usb:
        return "impossibile leggere VID:PID USB"
    exp = _expected_usb_ids_for_logical(int(logical))
    if usb not in exp:
        return f"VID:PID {usb[0]}:{usb[1]} non ammesso per log.{logical}"
    return None


def get_runtime_v4l_overrides() -> dict[int, int]:
    with _runtime_v4l_lock:
        return dict(_runtime_v4l_by_logical)


def set_runtime_v4l_overrides(updates: dict[int, int | None]) -> dict[str, Any]:
    """Applica o rimuove override runtime. Se ``GO2_VIDEO_INDEX_<n>`` è nell'ambiente, il relativo slot viene rifiutato."""
    applied: dict[str, Any] = {}
    errors: list[str] = []
    for raw_k, raw_v in updates.items():
        try:
            log = int(raw_k)
        except (TypeError, ValueError):
            errors.append(f"chiave logical non valida: {raw_k!r}")
            continue
        if log not in (0, 6):
            errors.append(f"log.{log} non supportato (solo 0 e 6)")
            continue
        env_key = f"GO2_VIDEO_INDEX_{log}"
        if raw_v is None:
            with _runtime_v4l_lock:
                _runtime_v4l_by_logical.pop(log, None)
            applied[str(log)] = None
            continue
        if env_key in os.environ:
            errors.append(
                f"log.{log}: {env_key} è impostato — rimuovi la variabile (o commentala in nx_dashboard_env.sh) per usare la selezione dalla dashboard."
            )
            continue
        try:
            idx = int(raw_v)
        except (TypeError, ValueError):
            errors.append(f"log.{log}: v4l_index non valido: {raw_v!r}")
            continue
        err = validate_runtime_v4l_for_logical(log, idx)
        if err:
            errors.append(f"log.{log}: {err}")
            continue
        with _runtime_v4l_lock:
            _runtime_v4l_by_logical[log] = idx
        applied[str(log)] = int(idx)
    return {"ok": len(errors) == 0, "applied": applied, "errors": errors}


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
        self._pause_until: dict[int, float] = {}

    def request_pause(self, device: int, duration_s: float = 0.5) -> None:
        """Rilascia temporaneamente il V4L su ``device`` (capture pyrealsense2 on-demand)."""
        until = time.time() + max(0.05, float(duration_s))
        with self._lock:
            self._pause_until[int(device)] = max(self._pause_until.get(int(device), 0.0), until)

    def _is_paused(self, device: int) -> bool:
        with self._lock:
            until = self._pause_until.get(int(device), 0.0)
        return time.time() < until

    def start(self, device: int | None = None) -> None:
        if cv2 is None:
            return
        devices = [device] if device is not None else list(self.devices)
        for dev in devices:
            if dev not in self.devices or dev in self._started_devices:
                continue
            self._started_devices.add(dev)
            threading.Thread(target=self._loop, args=(dev,), daemon=True).start()

    def _loop(self, device: int) -> None:
        cap = None
        last_opened_v4l: int | None = None
        orb_lease: orbbec_lock.OrbbecLease | None = None
        is_orbbec_logical = _logical_uses_orbbec(device)
        bad_rgb_streak = 0
        last_recover_ts = 0.0

        def _recover_realsense_rgb_mapping() -> bool:
            nonlocal last_recover_ts
            if int(device) != 0 or wrist_depth_backend() != "realsense":
                return False
            now = time.time()
            try:
                cooldown_s = float(os.environ.get("GO2_RGB_RECOVER_COOLDOWN_S", "3.0"))
            except ValueError:
                cooldown_s = 3.0
            if now - last_recover_ts < max(0.6, cooldown_s):
                return False
            cand = v4l_candidates_for_logical_slot(0)
            if len(cand) < 2:
                return False
            cur = int(last_opened_v4l) if last_opened_v4l is not None else _v4l_index_for_logical_camera(0)
            if cur not in cand:
                return False
            nxt = cand[(cand.index(cur) + 1) % len(cand)]
            if nxt == cur:
                return False
            env_key = "GO2_VIDEO_INDEX_0"
            if env_key in os.environ:
                with self._lock:
                    self.errors[device] = (
                        f"log.0 non RGB ma {env_key} e fisso su {os.environ.get(env_key)}; "
                        "rimuovi env per permettere auto-recovery runtime"
                    )
                return False
            with _runtime_v4l_lock:
                _runtime_v4l_by_logical[0] = int(nxt)
            last_recover_ts = now
            with self._lock:
                self.errors[device] = (
                    f"auto-recovery RGB log.0: switch /dev/video{cur} -> /dev/video{nxt} "
                    "(frame mono/IR persistenti)"
                )
            return True

        def _release_all() -> None:
            nonlocal cap, last_opened_v4l, orb_lease
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            cap = None
            last_opened_v4l = None
            if orb_lease is not None:
                orbbec_lock.release(orb_lease)
                orb_lease = None

        while not self._stop.is_set():
            start = time.perf_counter()
            if self._is_paused(device):
                _release_all()
                with self._lock:
                    self.errors[device] = "stream in pausa (capture depth on-demand)"
                time.sleep(0.15)
                continue
            # Prelazione cooperativa: se un altro consumatore (presa SDK / altro processo)
            # ha chiesto l'Orbbec, cediamo subito la camera e attendiamo che finisca.
            if is_orbbec_logical and cap is not None and orbbec_lock.preempt_requested():
                _release_all()
                with self._lock:
                    self.errors[device] = "Orbbec ceduto a una presa/altro processo (prelazione)"
                time.sleep(0.4)
                continue
            target_v4l = _v4l_index_for_logical_camera(device)
            hot_remap = os.environ.get("GO2_CAMERA_HOT_REMAP", "0").strip().lower() in {"1", "true", "yes", "on"}
            if hot_remap and (
                cap is not None
                and cap.isOpened()
                and last_opened_v4l is not None
                and int(target_v4l) != int(last_opened_v4l)
            ):
                _release_all()
            if cap is None or not cap.isOpened():
                if cap is not None:
                    _release_all()
                # Orbbec: prendi il lock cross-process PRIMA di aprire il device. Se è occupato
                # (o c'è una prelazione in corso) non aprire: aspetta senza rubare la camera.
                if is_orbbec_logical:
                    if orbbec_lock.preempt_requested():
                        with self._lock:
                            self.errors[device] = "Orbbec in uso da una presa/altro processo (attendo)"
                        time.sleep(0.5)
                        continue
                    orb_lease = orbbec_lock.acquire("camera_stream", blocking=True, timeout_s=2.5)
                    if orb_lease is None:
                        with self._lock:
                            self.errors[device] = (
                                "Orbbec occupato da altro processo"
                                + (f" ({orbbec_lock.holder_info()})" if orbbec_lock.holder_info() else "")
                                + " — stream in attesa"
                            )
                        time.sleep(0.6)
                        continue
                opened_v4l: int | None = None
                for v4l_try in v4l_open_candidates_for_logical(device):
                    cap_try = _cv_videocapture(int(v4l_try))
                    if cap_try.isOpened():
                        cap = cap_try
                        opened_v4l = int(v4l_try)
                        break
                    try:
                        cap_try.release()
                    except Exception:
                        pass
                if cap is None and orb_lease is not None:
                    orbbec_lock.release(orb_lease)
                    orb_lease = None
                if cap is not None and cap.isOpened() and opened_v4l is not None:
                    v4l_idx = opened_v4l
                    last_opened_v4l = int(v4l_idx)
                    try:
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    except Exception:
                        pass
                    usb = _usb_vid_pid_for_video_index(v4l_idx)
                    if usb in _USB_IDS_REALSENSE:
                        _try_set_uvc_mjpeg_fourcc(cap, prefer_env="GO2_REALSENSE_PREFER_MJPEG")
                    elif int(device) == 0 and usb in _USB_IDS_LOGICAL_0:
                        _try_set_uvc_mjpeg_fourcc(cap, prefer_env="GO2_ORBBEC_PREFER_MJPEG")
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    cap.set(cv2.CAP_PROP_FPS, 15)
                else:
                    cap = None
                    last_opened_v4l = None
                    tried = v4l_open_candidates_for_logical(device)
                    v4l_idx = int(tried[0]) if tried else int(target_v4l)
                    hint = _v4l_permission_hint(v4l_idx)
                    msg = (
                        f"open failed (V4L candidati {tried}, logical {device})"
                        if len(tried) > 1
                        else f"open failed (V4L /dev/video{v4l_idx}, logical {device})"
                    )
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
                _release_all()
                time.sleep(0.5)
                continue

            if ok and frame is not None:
                if frame.size and float(frame.max()) < 4.0:
                    with self._lock:
                        self.errors[device] = (
                            f"frame nero su V4L — verifica GO2_VIDEO_INDEX_{device} "
                            f"(reale /dev/video{_v4l_index_for_logical_camera(device)}, logico {device})"
                        )
                    time.sleep(0.25)
                    continue
                enc_ok, jpg = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
                )
                if enc_ok:
                    v4l_now = int(last_opened_v4l if last_opened_v4l is not None else target_v4l)
                    usb_now = _usb_vid_pid_for_video_index(v4l_now)
                    diag = _frame_rgb_diagnostics(frame)
                    if int(device) == 0 and usb_now == ("2bc5", "080b"):
                        cm = _orbbec_probe_chroma_min()
                        me = _orbbec_max_edge_from_env(relaxed=False)
                        plausible = _frame_orbbec_color_plausible(frame, cm, max_edge_den=me)
                        diag = {
                            "color_chroma": round(float(_frame_channel_chroma_bgr(frame)), 3),
                            "rgb_like": plausible,
                            "stream_kind": "rgb" if plausible else "mono_or_ir",
                        }
                    with self._lock:
                        self.frames[device] = {
                            "jpg": jpg.tobytes(),
                            "ts": time.time(),
                            "shape": list(frame.shape),
                            "label": self.devices[device],
                            "color_chroma": diag.get("color_chroma"),
                            "rgb_like": bool(diag.get("rgb_like")),
                            "stream_kind": diag.get("stream_kind"),
                        }
                        self.errors.pop(device, None)
                    usb_now = _usb_vid_pid_for_video_index(v4l_now)
                    if (
                        int(device) == 0
                        and usb_now in _wrist_realsense_usb_ids()
                        and not bool(diag.get("rgb_like"))
                    ):
                        bad_rgb_streak += 1
                        try:
                            thr = int(os.environ.get("GO2_RGB_RECOVER_STREAK", "24"))
                        except ValueError:
                            thr = 24
                        if bad_rgb_streak >= max(6, thr):
                            if _recover_realsense_rgb_mapping():
                                bad_rgb_streak = 0
                                _release_all()
                                time.sleep(0.25)
                                continue
                    else:
                        bad_rgb_streak = 0
            else:
                with self._lock:
                    self.errors[device] = "read returned no frame"
                bad_rgb_streak = 0
                _release_all()
                time.sleep(0.5)
                continue
            delay = self.period - (time.perf_counter() - start)
            if delay > 0:
                time.sleep(delay)
        _release_all()

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
                    "error": self.errors.get(device),
                }
                for device in self.devices
            }


CAMERA_CACHE = CameraCache(
    CAMERA_DEVICES,
    fps=float(os.environ.get("GO2_CAMERA_CACHE_FPS", "20")),
)

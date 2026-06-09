"""Bridge Hermes → Sport DDS + JPEG camere (operator :5052 opzionale)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from go2_dashboard.hermes.context import d1_jog_base, operator_base, operator_reachable_quick

_ROOT = Path(__file__).resolve().parent.parent.parent
_SPORT_ONCE = _ROOT / "scripts" / "sport_accompany_once.py"


def _post_json(
    base: str,
    path: str,
    body: dict[str, Any],
    *,
    timeout_s: float = 8.0,
) -> tuple[dict[str, Any], int]:
    url = base.rstrip("/") + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            code = int(getattr(resp, "status", 200))
            raw = resp.read().decode("utf-8", errors="replace")
            return (json.loads(raw) if raw.strip() else {}), code
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {"ok": False, "reason": raw[:500]}
        return payload, int(exc.code)
    except Exception as exc:
        return {"ok": False, "reason": repr(exc)}, 0


def _get_bytes(base: str, path: str, *, timeout_s: float = 6.0) -> tuple[bytes | None, int]:
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Accept": "image/jpeg"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.read(), int(getattr(resp, "status", 200))
    except urllib.error.HTTPError as exc:
        return None, int(exc.code)
    except Exception:
        return None, 0


def _run_sport_subprocess(cmd: list[str], *, label: str) -> dict[str, Any]:
    if not _SPORT_ONCE.is_file():
        return {"ok": False, "reason": "missing sport_accompany_once.py", "via": "direct"}
    env = os.environ.copy()
    env.setdefault("GO2_ENABLE_BASE_MOTION", "1")
    env.setdefault("GO2_LOCAL", "1")
    timeout_s = float(os.environ.get("HERMES_SPORT_TIMEOUT_S", "55"))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"sport_direct_timeout_{timeout_s}s", "via": "direct", "label": label}
    stderr = (proc.stderr or "")[-2000:]
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        return {
            "ok": False,
            "via": "direct",
            "label": label,
            "reason": f"exit_{proc.returncode}",
            "stderr_tail": stderr[-800:],
        }
    try:
        out: dict[str, Any] = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {
            "ok": False,
            "via": "direct",
            "label": label,
            "reason": "bad_json",
            "stdout": (proc.stdout or "")[:1500],
            "stderr_tail": stderr[-500:],
        }
    out["via"] = "direct"
    out["label"] = label
    if proc.returncode != 0 and not out.get("ok"):
        out["subprocess_returncode"] = proc.returncode
    return out


def sport_command_direct(
    mode: str,
    *,
    stand_up_first: bool = False,
) -> dict[str, Any]:
    """Sport DDS sullo stesso host Hermes (NX) — non richiede operator :5052."""
    cmd = [
        sys.executable,
        str(_SPORT_ONCE),
        "--mode",
        mode,
        "--enable",
        "1",
        "--stand-up-first",
        "1" if stand_up_first else "0",
    ]
    out = _run_sport_subprocess(cmd, label=f"accompany:{mode}")
    out["mode"] = mode
    return out


def sport_move_command(
    *,
    vx: float,
    vy: float,
    vyaw: float,
    duration_s: float,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(_SPORT_ONCE),
        "--mode",
        "move",
        "--vx",
        str(vx),
        "--vy",
        str(vy),
        "--vyaw",
        str(vyaw),
        "--duration",
        str(duration_s),
    ]
    out = _run_sport_subprocess(cmd, label="move")
    out["vx"] = vx
    out["vy"] = vy
    out["vyaw"] = vyaw
    out["duration_s"] = duration_s
    return out


def sport_simple_command(action: str) -> dict[str, Any]:
    act = str(action).strip().lower()
    cmd = [sys.executable, str(_SPORT_ONCE), "--mode", act]
    out = _run_sport_subprocess(cmd, label=f"simple:{act}")
    out["action"] = act
    return out


def sport_command_http(
    mode: str,
    *,
    sync: bool | None = None,
    stand_up_first: bool = False,
) -> dict[str, Any]:
    """Fallback: POST /api/base/accompany_mode sulla dashboard operator."""
    if sync is None:
        sync = os.environ.get("HERMES_SPORT_SYNC", "1").lower() in {"1", "true", "yes", "on"}
    timeout_s = float(os.environ.get("HERMES_SPORT_TIMEOUT_S", "50" if sync else "8"))
    body: dict[str, Any] = {
        "mode": mode,
        "enable": True,
        "stand_up_first": stand_up_first,
        "sync": sync,
    }
    payload, code = _post_json(operator_base(), "/api/base/accompany_mode", body, timeout_s=timeout_s)
    payload["http_status"] = code
    payload["via"] = "http"
    payload["mode"] = mode
    return payload


def sport_command(
    mode: str,
    *,
    sync: bool | None = None,
    stand_up_first: bool = False,
) -> dict[str, Any]:
    if sync is None:
        sync = os.environ.get("HERMES_SPORT_SYNC", "0").lower() in {"1", "true", "yes", "on"}
    prefer_direct = os.environ.get("HERMES_SPORT_DIRECT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if prefer_direct or not operator_reachable_quick():
        out = sport_command_direct(mode)
        if out.get("ok"):
            return out
        if operator_reachable_quick():
            http_out = sport_command_http(mode, sync=sync, stand_up_first=stand_up_first)
            http_out["direct_attempt"] = out
            return http_out
        out["operator_unreachable"] = True
        return out
    return sport_command_http(mode, sync=sync, stand_up_first=stand_up_first)


def _fetch_orbbec_via_d1_jog(*, timeout_s: float) -> tuple[bytes | None, dict[str, Any]]:
    meta: dict[str, Any] = {"via": "d1_jog_http", "base": d1_jog_base()}
    payload, code = _post_json(d1_jog_base(), "/api/orbbec/capture", {}, timeout_s=timeout_s)
    meta["http_status"] = code
    meta["capture"] = payload
    if not payload.get("ok"):
        return None, meta
    jpg, img_code = _get_bytes(d1_jog_base(), "/api/orbbec/last.jpg", timeout_s=timeout_s)
    meta["jpg_http_status"] = img_code
    meta["bytes"] = len(jpg) if jpg else 0
    return jpg, meta


def _fetch_orbbec_inprocess() -> tuple[bytes | None, dict[str, Any]]:
    meta: dict[str, Any] = {"via": "orbbec_inprocess", "device": 0}
    try:
        from go2_dashboard.d1_jog import orbbec_capture

        out = orbbec_capture.capture_orbbec_jpeg()
        meta["capture"] = out
        if not out.get("ok"):
            return None, meta
        snap = orbbec_capture.latest_snapshot_path()
        if snap is None or not snap.is_file():
            return None, meta
        data = snap.read_bytes()
        meta["bytes"] = len(data)
        meta["v4l_index"] = out.get("v4l_index")
        return data, meta
    except Exception as exc:
        meta["error"] = repr(exc)
        return None, meta


def _fetch_camera_cache(device: int, *, wait_s: float) -> tuple[bytes | None, dict[str, Any]]:
    meta: dict[str, Any] = {"via": "camera_cache", "device": device}
    try:
        from go2_dashboard.cameras import CAMERA_CACHE
        from go2_dashboard.operator_stack import go2_local

        if not go2_local():
            meta["reason"] = "GO2_LOCAL!=1"
            return None, meta
        CAMERA_CACHE.start(device)
        jpg = CAMERA_CACHE.get_jpeg(device, wait_s=wait_s)
        meta["bytes"] = len(jpg) if jpg else 0
        return jpg, meta
    except Exception as exc:
        meta["error"] = repr(exc)
        return None, meta


def fetch_camera_jpeg_local(device: int, *, wait_s: float | None = None) -> tuple[bytes | None, dict[str, Any]]:
    """JPEG senza operator :5052 — Orbbec via D1/in-process, frontale via cache V4L."""
    wait = wait_s if wait_s is not None else float(os.environ.get("HERMES_CAMERA_WAIT_S", "2.5"))
    timeout_s = max(8.0, wait + 4.0)

    if int(device) == 0:
        for fetcher in (_fetch_orbbec_inprocess, lambda: _fetch_orbbec_via_d1_jog(timeout_s=timeout_s)):
            jpg, meta = fetcher()
            if jpg:
                return jpg, meta
        jpg, meta = _fetch_camera_cache(0, wait_s=wait)
        if jpg:
            return jpg, meta
        return None, meta

    jpg, meta = _fetch_camera_cache(device, wait_s=wait)
    if jpg:
        return jpg, meta
    return None, meta


def ensure_cameras_warm(device: int | None = None) -> dict[str, Any]:
    if operator_reachable_quick():
        payload, code = _post_json(operator_base(), "/api/nx/stack/start", {}, timeout_s=12.0)
        payload["http_status"] = code
        if device is not None:
            payload["device"] = device
        return payload
    meta: dict[str, Any] = {"ok": True, "via": "local", "device": device}
    if device is not None:
        _fetch_camera_cache(device, wait_s=0.5)
    else:
        for dev in (0, 6):
            _fetch_camera_cache(dev, wait_s=0.5)
    return meta


def fetch_camera_jpeg(device: int, *, wait_s: float | None = None) -> tuple[bytes | None, dict[str, Any]]:
    wait = wait_s if wait_s is not None else float(os.environ.get("HERMES_CAMERA_WAIT_S", "2.5"))
    bust = int(time.time() * 1000)
    path = f"/api/robot/camera/{device}.jpg?t={bust}"
    timeout_s = max(4.0, wait + 2.0)

    if wait > 0:
        ensure_cameras_warm(device)

    if operator_reachable_quick():
        meta: dict[str, Any] = {"device": device, "path": path, "via": "operator_http"}
        _get_bytes(operator_base(), path, timeout_s=timeout_s)
        jpg, code = _get_bytes(operator_base(), path, timeout_s=timeout_s)
        meta["http_status"] = code
        meta["bytes"] = len(jpg) if jpg else 0
        meta["fetched_at"] = time.time()
        if jpg:
            return jpg, meta

    jpg, meta = fetch_camera_jpeg_local(device, wait_s=wait)
    meta["fetched_at"] = time.time()
    return jpg, meta

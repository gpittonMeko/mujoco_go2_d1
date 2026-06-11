"""
OpenVLA: stub, adapter esterno, server HTTP ``/act`` locale (``OPENVLA_ACT_SERVER_URL``),
oppure inferenza Hugging Face (``OPENVLA_USE_HF=1``).

Per HF si usa ``AutoModelForVision2Seq`` + ``predict_action`` come nel README ufficiale openvla/openvla.
"""
from __future__ import annotations

import base64
import binascii
import importlib
import io
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_HF_LOCK = threading.Lock()
_HF_STATE: dict[str, Any] = {
    "processor": None,
    "model": None,
    "device": None,
    "dtype": None,
    "model_id": None,
    "load_error": None,
}


def _stub_mode() -> bool:
    return os.environ.get("OPENVLA_RUNTIME_STUB", "0").lower() in {"1", "true", "yes", "on"}


def _hf_mode() -> bool:
    return os.environ.get("OPENVLA_USE_HF", "0").lower() in {"1", "true", "yes", "on"}


def _act_server_base() -> str | None:
    """Base URL server OpenVLA separato (es. ``http://127.0.0.1:8000``) — solo LAN locale consigliata."""
    raw = (os.environ.get("OPENVLA_ACT_SERVER_URL") or "").strip().rstrip("/")
    return raw or None


def _act_path() -> str:
    p = (os.environ.get("OPENVLA_ACT_PATH") or "/act").strip()
    if not p.startswith("/"):
        p = "/" + p
    return p


def _hf_model_id() -> str:
    return (
        (os.environ.get("OPENVLA_HF_MODEL_ID") or "").strip()
        or (os.environ.get("OPENVLA_CHECKPOINT") or "").strip()
        or "openvla/openvla-7b"
    )


def _torch_cuda_ok() -> tuple[bool, str | None]:
    try:
        import torch

        if not torch.cuda.is_available():
            return False, "torch presente ma CUDA non disponibile (OpenVLA HF richiede GPU)"
        return True, None
    except Exception as exc:
        return False, repr(exc)


def _checkpoint_path() -> Path | None:
    raw = (os.environ.get("OPENVLA_CHECKPOINT") or "").strip()
    if not raw:
        return None
    return Path(raw)


def _repo_root_path() -> Path | None:
    raw = (os.environ.get("OPENVLA_REPO_ROOT") or "").strip()
    if not raw:
        return None
    return Path(raw)


def openvla_status() -> dict[str, Any]:
    ck = _checkpoint_path()
    rr = _repo_root_path()
    torch_ok, torch_err = _torch_cuda_ok()
    adapter = (os.environ.get("OPENVLA_ADAPTER_CALLABLE") or "").strip()
    mid = _hf_model_id()
    act_b = _act_server_base()
    act_p = _act_path()
    with _HF_LOCK:
        loaded = _HF_STATE["model"] is not None
        le = _HF_STATE.get("load_error")
    priority = "stub" if _stub_mode() else None
    if priority is None and adapter:
        priority = "adapter"
    if priority is None and act_b:
        priority = "act_http"
    if priority is None and _hf_mode():
        priority = "hf"
    if priority is None:
        priority = "(none)"
    return {
        "runtime_stub": _stub_mode(),
        "plan_mode_priority_it": (
            "stub → OPENVLA_ADAPTER_CALLABLE → OPENVLA_ACT_SERVER_URL → OPENVLA_USE_HF "
            f"(attivo previsto: {priority})"
        ),
        "act_server_url": act_b,
        "act_path": act_p if act_b else None,
        "use_hf": _hf_mode(),
        "hf_model_id": mid if _hf_mode() else None,
        "hf_model_loaded": loaded,
        "hf_load_error": le,
        "openvla_checkpoint": str(ck) if ck else None,
        "checkpoint_exists": bool(ck and ck.exists()),
        "openvla_repo_root": str(rr) if rr else None,
        "repo_root_exists": bool(rr and rr.is_dir()),
        "openvla_adapter_callable": adapter or None,
        "torch_cuda_ok": torch_ok,
        "torch_cuda_error": torch_err,
    }


def _parse_adapter_callable() -> tuple[str, str] | None:
    raw = (os.environ.get("OPENVLA_ADAPTER_CALLABLE") or "").strip()
    if not raw or ":" not in raw:
        return None
    mod_name, _, fn_name = raw.partition(":")
    mod_name, fn_name = mod_name.strip(), fn_name.strip()
    if not mod_name or not fn_name:
        return None
    return mod_name, fn_name


def _try_load_plan_fn() -> tuple[Callable[..., Any] | None, str | None]:
    parsed = _parse_adapter_callable()
    if not parsed:
        return None, None
    mod_name, fn_name = parsed
    rr = _repo_root_path()
    if rr and rr.is_dir():
        root_s = str(rr)
        if root_s not in sys.path:
            sys.path.insert(0, root_s)
    repo_s = str(REPO_ROOT)
    if repo_s not in sys.path:
        sys.path.insert(0, repo_s)
    try:
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, fn_name, None)
        if not callable(fn):
            return None, f"{mod_name}:{fn_name} non è callable"
        return fn, None
    except Exception as exc:
        return None, repr(exc)


def _hf_imports_ok() -> tuple[bool, str | None]:
    try:
        import torch  # noqa: F401
        from PIL import Image  # noqa: F401
        from transformers import AutoModelForVision2Seq, AutoProcessor  # noqa: F401
    except Exception as exc:
        return False, repr(exc)
    return True, None


def openvla_import_ok() -> tuple[bool, str | None]:
    if _stub_mode():
        return True, None
    fn, err = _try_load_plan_fn()
    if fn is not None:
        return True, None
    if err:
        return False, f"adapter_load_failed: {err}"
    if _act_server_base():
        return True, None
    if _hf_mode():
        ok_cuda, cerr = _torch_cuda_ok()
        if not ok_cuda:
            return False, cerr
        ok_imp, ierr = _hf_imports_ok()
        if not ok_imp:
            return (
                False,
                f"{ierr} — installa: pip install -r external/openvla_worker/requirements-openvla.txt "
                "(e torch con CUDA da pytorch.org).",
            )
        return True, None
    return (
        False,
        "Imposta OPENVLA_RUNTIME_STUB=1, OPENVLA_ACT_SERVER_URL (server /act locale), "
        "OPENVLA_USE_HF=1 (Hugging Face), oppure OPENVLA_ADAPTER_CALLABLE=modulo:funzione. "
        "Vedi external/openvla_worker/README.md.",
    )


def _ensure_hf_vla() -> str | None:
    """Carica modello HF una sola volta. Ritorna messaggio errore o None."""
    with _HF_LOCK:
        if _HF_STATE["model"] is not None:
            return None
        if _HF_STATE.get("load_error"):
            return str(_HF_STATE["load_error"])
        try:
            import torch
            from transformers import AutoModelForVision2Seq, AutoProcessor

            ok_cuda, cerr = _torch_cuda_ok()
            if not ok_cuda:
                _HF_STATE["load_error"] = cerr
                return cerr

            mid = _hf_model_id()
            device = torch.device("cuda:0")
            if torch.cuda.is_bf16_supported():
                dtype = torch.bfloat16
            else:
                dtype = torch.float16

            processor = AutoProcessor.from_pretrained(mid, trust_remote_code=True)
            load_kw: dict[str, Any] = dict(
                trust_remote_code=True,
                torch_dtype=dtype,
                low_cpu_mem_usage=True,
            )
            try:
                model = AutoModelForVision2Seq.from_pretrained(
                    mid, attn_implementation="sdpa", **load_kw
                ).to(device)
            except Exception:
                model = AutoModelForVision2Seq.from_pretrained(mid, **load_kw).to(device)
            model.eval()

            mid_path = Path(mid)
            if mid_path.is_dir():
                stats_p = mid_path / "dataset_statistics.json"
                if stats_p.is_file():
                    import json

                    model.norm_stats = json.loads(stats_p.read_text(encoding="utf-8"))

            _HF_STATE["processor"] = processor
            _HF_STATE["model"] = model
            _HF_STATE["device"] = device
            _HF_STATE["dtype"] = dtype
            _HF_STATE["model_id"] = mid
            _HF_STATE["load_error"] = None
            return None
        except Exception as exc:
            err = repr(exc)
            _HF_STATE["load_error"] = err
            return err


def _fetch_jpeg(url: str, timeout_s: float = 20.0) -> bytes:
    req = Request(url, headers={"User-Agent": "go2-openvla-worker/1.0"})
    with urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def _decode_jpeg_b64(raw: Any) -> bytes | None:
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or s.startswith("embedded://"):
        return None
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    try:
        return base64.standard_b64decode(s, validate=False)
    except (ValueError, binascii.Error):
        return None


def _jpeg_from_body(body: dict[str, Any]) -> tuple[bytes | None, str]:
    """JPEG inline dalla NX (cloud AWS). Preferisce polso (cam 0)."""
    logical = body.get("logical_camera_device", 0)
    url = (
        body.get("image_url")
        or body.get("camera_jpg_url")
        or os.environ.get("WORKER_CAMERA_JPG_URL")
        or f"embedded://camera/{logical}"
    )
    if not isinstance(url, str):
        url = f"embedded://camera/{logical}"

    for key in ("jpeg_base64", "image_jpeg_b64", "jpeg_base64_wrist"):
        got = _decode_jpeg_b64(body.get(key))
        if got:
            return got, url

    return None, url


def _gripper_command_from_action(act_list: list[float]) -> str:
    """7° DoF OpenVLA bridge: gripper assoluto 0=open, 1=closed."""
    if len(act_list) < 7:
        return "hold"
    try:
        g = float(act_list[6])
    except (TypeError, ValueError):
        return "hold"
    if g >= 0.55:
        return "close"
    if g <= 0.35:
        return "open"
    return "hold"


def _attach_gripper_and_cameras(body: dict[str, Any], out: dict[str, Any], act_list: list[float]) -> None:
    out["gripper_command"] = _gripper_command_from_action(act_list)
    if body.get("jpeg_base64_front") or body.get("jpeg_base64_wrist"):
        out["dual_camera_embedded"] = True
        out["logical_camera_device"] = body.get("logical_camera_device", 0)


def _openvla_fk_joints_active() -> bool:
    return os.environ.get("OPENVLA_ACTION_FK_JOINTS", "0").lower() in {"1", "true", "yes", "on"}


def _attach_openvla_joint_meta(out: dict[str, Any], act_list: list[float]) -> None:
    """Se il worker gira con ``OPENVLA_ACTION_FK_JOINTS=1``, i primi 6 numeri sono q D1 in radianti."""
    if not _openvla_fk_joints_active() or len(act_list) < 6:
        return
    out["openvla_joint_space"] = "d1_rad"
    out["openvla_target_joints_rad"] = [float(act_list[i]) for i in range(6)]


def _maybe_openvla_fk_tool_tip_base_link_m(vec: np.ndarray) -> list[float] | None:
    """
    Se ``OPENVLA_ACTION_FK_JOINTS=1``, interpreta i primi 6 valori dell'azione come **q (rad)** assoluti
    nel modello D1 ``arm_kinematics_d1_template`` e restituisce la punta utensile in **base_link**
    (stessa convenzione della dashboard / ``scene_3d`` operator).
    """
    if os.environ.get("OPENVLA_ACTION_FK_JOINTS", "0").lower() not in {"1", "true", "yes", "on"}:
        return None
    a = np.asarray(vec, dtype=np.float64).reshape(-1)
    if a.size < 6:
        return None
    s_scripts = str(REPO_ROOT / "scripts")
    if s_scripts not in sys.path:
        sys.path.insert(0, s_scripts)
    try:
        from arm_kinematics_d1_template import J_LIMITS, clamp, fk_tool_tip
    except Exception:
        return None
    try:
        q_pol = [clamp(float(a[i]), *J_LIMITS[i]) for i in range(6)]
    except (TypeError, ValueError):
        return None
    tip_arm = fk_tool_tip(q_pol)
    mount = np.array([0.15, 0.0, 0.06], dtype=float)
    bl = np.asarray(tip_arm, dtype=float).reshape(3) + mount
    return [round(float(bl[i]), 5) for i in range(3)]


def _heuristic_grasp_from_action(action: np.ndarray) -> list[float]:
    """Euristica solo per UI 3D: origine + scala * prime 3 componenti azione."""
    raw = (os.environ.get("OPENVLA_HEURISTIC_ORIGIN_M") or "0.42,0.0,0.18").strip()
    try:
        origin = np.array([float(x) for x in raw.split(",")], dtype=np.float64)
    except Exception:
        origin = np.array([0.42, 0.0, 0.18], dtype=np.float64)
    try:
        scale = float((os.environ.get("OPENVLA_HEURISTIC_ACTION_SCALE") or "0.04").strip())
    except ValueError:
        scale = 0.04
    a = np.asarray(action, dtype=np.float64).reshape(-1)
    if a.size < 3:
        return [float(origin[0]), float(origin[1]), float(origin[2])]
    delta = a[:3] * scale
    out = origin[:3] + delta
    return [float(out[0]), float(out[1]), float(out[2])]


def _openvla_ui_visuals_from_action(
    vec: np.ndarray,
    raw_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """UI: bbox normalizzato + heatmap gaussiana **euristica** da `action[0:2]` (non è attention VLM).

    Se il server ``/act`` o l'adapter restituiscono chiavi esplicite, vengono propagate:
    ``bbox_xyxy``, ``bbox``, ``heatmap_b64``, ``operators_debug_bbox_norm``, ``openvla_debug``.
    """
    a = np.asarray(vec, dtype=np.float64).reshape(-1)
    dbg: dict[str, Any] = {
        "action_dim": int(a.size),
        "action_preview_7": [round(float(a[i]), 5) for i in range(min(7, a.size))],
        "action_l2": round(float(np.linalg.norm(a)), 6),
        "note_it": (
            "BBox/heatmap qui sono **euristica** dai primi valori dell'azione (o pass-through dal server). "
            "Non sono detection 2D né saliency del Transformer salvo chiavi dal tuo backend."
        ),
    }
    try:
        cscale = float(os.environ.get("OPENVLA_UI_BBOX_CENTER_SCALE", "0.22"))
        bw = float(os.environ.get("OPENVLA_UI_BBOX_W", "0.30"))
        bh = float(os.environ.get("OPENVLA_UI_BBOX_H", "0.24"))
    except ValueError:
        cscale, bw, bh = 0.22, 0.30, 0.24
    if a.size >= 2:
        cx = float(0.5 + np.tanh(a[0]) * cscale)
        cy = float(0.5 + np.tanh(a[1]) * cscale)
    else:
        cx, cy = 0.5, 0.5
    wn, hn = float(min(bw, 0.92)), float(min(bh, 0.92))
    x0 = max(0.0, min(1.0 - wn, cx - wn / 2))
    y0 = max(0.0, min(1.0 - hn, cy - hn / 2))
    sigma = float(os.environ.get("OPENVLA_UI_HEATMAP_SIGMA", "0.14") or "0.14")

    pack: dict[str, Any] = {
        "operators_debug_bbox_norm": {
            "x": round(x0, 4),
            "y": round(y0, 4),
            "w": round(wn, 4),
            "h": round(hn, 4),
            "cx": round(cx, 4),
            "cy": round(cy, 4),
            "label": "openvla_heuristic",
        },
        "openvla_heatmap_gaussian": {
            "cx": round(cx, 4),
            "cy": round(cy, 4),
            "sigma": round(sigma, 4),
            "note_it": "Gaussiana 2D da azione[0:1] + tanh; contrasto per overlay.",
        },
        "openvla_debug": dbg,
    }

    if isinstance(raw_response, dict):
        if "openvla_debug" in raw_response and isinstance(raw_response["openvla_debug"], dict):
            merged = dict(dbg)
            merged.update(raw_response["openvla_debug"])
            pack["openvla_debug"] = merged
        for k in (
            "operators_debug_bbox_norm",
            "openvla_heatmap_gaussian",
            "openvla_heatmap_png_b64",
            "openvla_bbox_xyxy_pixels",
            "openvla_server_debug",
        ):
            if k in raw_response:
                pack[k] = raw_response[k]
        if "bbox_xyxy" in raw_response:
            pack["openvla_bbox_xyxy_pixels"] = raw_response["bbox_xyxy"]
        if "bbox" in raw_response:
            pack["openvla_bbox_norm"] = raw_response["bbox"]
        if "heatmap_b64" in raw_response:
            pack["openvla_heatmap_png_b64"] = raw_response["heatmap_b64"]
    return pack


def _merge_openvla_ui_into_plan(plan: dict[str, Any], vec: np.ndarray, raw: dict[str, Any] | None) -> None:
    if os.environ.get("OPENVLA_UI_OVERLAY", "1").lower() in {"0", "false", "no", "off"}:
        return
    vis = _openvla_ui_visuals_from_action(vec, raw)
    for k, v in vis.items():
        plan[k] = v
    u = float(plan["operators_debug_bbox_norm"]["cx"])
    v = float(plan["operators_debug_bbox_norm"]["cy"])
    plan["operators_overlay_points"] = [{"x": u, "y": v, "label": "openvla_hint"}]
    plan["grip_point"] = {"u": u, "v": v, "cx": int(round(u * 640)), "cy": int(round(v * 480))}


def _extract_action_vector(data: Any, _depth: int = 0) -> np.ndarray | None:
    """Estrae un vettore numerico 1D da risposte JSON tipiche del server /act."""
    if data is None or _depth > 6:
        return None
    if isinstance(data, (list, tuple)):
        if not data:
            return None
        if all(not isinstance(x, (list, tuple, dict)) for x in data):
            try:
                return np.asarray([float(x) for x in data], dtype=np.float64).reshape(-1)
            except (TypeError, ValueError):
                return None
        return _extract_action_vector(data[0], _depth + 1)
    if isinstance(data, dict):
        for key in ("action", "actions", "output", "prediction", "result", "openvla_action", "delta", "cmd", "vector"):
            if key in data:
                got = _extract_action_vector(data[key], _depth + 1)
                if got is not None and got.size:
                    return got
    return None


def _plan_act_http(
    body: dict[str, Any],
    image_jpeg_url: str,
    base: str,
    jpeg_bytes: bytes | None = None,
) -> dict[str, Any]:
    """POST JSON verso server OpenVLA separato (tipicamente ``127.0.0.1:8000/act``)."""
    path = _act_path()
    full = base + path
    if jpeg_bytes is None:
        try:
            jpeg = _fetch_jpeg(
                image_jpeg_url,
                timeout_s=float(os.environ.get("OPENVLA_ACT_FETCH_TIMEOUT_S", "25")),
            )
        except (URLError, OSError, TimeoutError) as exc:
            return {"ok": False, "reason": "image_fetch_failed", "detail": repr(exc), "image_url": image_jpeg_url}
    else:
        jpeg = jpeg_bytes

    inst = str(
        body.get("instruction")
        or body.get("language_instruction")
        or body.get("task")
        or os.environ.get("OPENVLA_DEFAULT_INSTRUCTION")
        or "grasp the object"
    ).strip()

    img_key = (os.environ.get("OPENVLA_ACT_JSON_IMAGE_KEY") or "image").strip() or "image"
    inst_key = (os.environ.get("OPENVLA_ACT_JSON_INSTRUCTION_KEY") or "instruction").strip() or "instruction"
    use_url = os.environ.get("OPENVLA_ACT_USE_IMAGE_URL", "0").lower() in {"1", "true", "yes", "on"}
    url_key = (os.environ.get("OPENVLA_ACT_JSON_IMAGE_URL_KEY") or "image_url").strip() or "image_url"

    payload: dict[str, Any] = {inst_key: inst}
    if use_url:
        payload[url_key] = str(body.get("image_url") or body.get("camera_jpg_url") or image_jpeg_url)
    else:
        payload[img_key] = base64.standard_b64encode(jpeg).decode("ascii")

    extra = body.get("act_server_extra")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in payload:
                payload[k] = v

    data_bytes = json.dumps(payload).encode("utf-8")
    timeout = float(os.environ.get("OPENVLA_ACT_TIMEOUT_S", "120"))
    req = Request(
        full,
        data=data_bytes,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "go2-openvla-worker/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return {
            "ok": False,
            "reason": "act_server_http_error",
            "status_code": e.code,
            "detail": err_body[:2000],
            "act_url": full,
        }
    except URLError as e:
        return {"ok": False, "reason": "act_server_unreachable", "detail": repr(e), "act_url": full}

    try:
        resp_json: Any = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        return {"ok": False, "reason": "act_server_bad_json", "detail": repr(e), "_raw": raw[:800]}

    if isinstance(resp_json, dict) and resp_json.get("ok") is False:
        out = dict(resp_json)
        out.setdefault("backend", "openvla_act_http")
        return out

    vec = _extract_action_vector(resp_json)
    if vec is None or vec.size == 0:
        return {
            "ok": False,
            "reason": "act_server_no_action_vector",
            "hint_it": (
                "Nessun vettore azione riconosciuto nella risposta; estendi OPENVLA_ACT_JSON_* o passa "
                "``act_server_extra`` nel JSON del piano. Vedi README."
            ),
            "act_raw_response": resp_json if isinstance(resp_json, dict) else str(resp_json)[:800],
            "backend": "openvla_act_http",
        }

    act_list = [float(x) for x in vec.tolist()]
    grasp = _heuristic_grasp_from_action(vec)
    fk_bl = _maybe_openvla_fk_tool_tip_base_link_m(vec)
    if fk_bl is not None:
        grasp = fk_bl

    out: dict[str, Any] = {
        "ok": True,
        "backend": "openvla_act_http",
        "hint_it": (
            "Azione da OPENVLA_ACT_SERVER_URL; ``grasp_display_base_link_m`` è euristica (OPENVLA_HEURISTIC_*) "
            "salvo ``OPENVLA_ACTION_FK_JOINTS=1`` (primi 6 = q rad → FK punta in base_link). "
            "Esporre solo :8765 verso la NX; il server /act resti su localhost."
        ),
        "openvla_action_7dof": act_list,
        "grasp_display_base_link_m": grasp,
        "operators_grasp_points_base_link_m": [grasp, [grasp[0] + 0.01, grasp[1], grasp[2]]],
        "grip_point": {"cx": 320, "cy": 240, "u": 0.5, "v": 0.5},
        "image_url_used": image_jpeg_url,
        "act_url": full,
    }
    if fk_bl is not None:
        out["openvla_fk_tool_tip_base_link_m"] = fk_bl
    _attach_openvla_joint_meta(out, act_list)
    _merge_openvla_ui_into_plan(out, vec, resp_json if isinstance(resp_json, dict) else None)
    _attach_gripper_and_cameras(body, out, act_list)
    if isinstance(resp_json, dict):
        for keep in ("confidence", "message", "debug", "policy_version"):
            if keep in resp_json:
                out[f"act_{keep}"] = resp_json[keep]
    return out


def _stub_plan_from_image_url(url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    pts = [[0.40, 0.06, 0.17], [0.41, 0.05, 0.18], [0.39, 0.07, 0.16]]
    stub_vec = np.array([0.12, -0.18, 0.06, 0.0, 0.0, 0.0, 0.85], dtype=np.float64)
    out: dict[str, Any] = {
        "ok": True,
        "backend": "openvla_runtime_stub",
        "hint_it": "OPENVLA_RUNTIME_STUB=1 — nessun modello VLM; solo presa sintetica per test UI.",
        "openvla_action_7dof": [float(x) for x in stub_vec.tolist()],
        "grasp_display_base_link_m": [0.40, 0.06, 0.17],
        "operators_grasp_points_base_link_m": pts,
        "grip_point": {"cx": 320, "cy": 240, "u": 0.5, "v": 0.5},
        "operators_overlay_points": [{"x": 0.5, "y": 0.5, "label": "openvla_stub"}],
        "image_url_used": url,
    }
    _merge_openvla_ui_into_plan(
        out,
        stub_vec,
        {"openvla_debug": {"stub_runtime": True, "note_it": "Vettore azione finto solo per provare overlay UI"}},
    )
    if body:
        _attach_gripper_and_cameras(body, out, [float(x) for x in stub_vec.tolist()])
    else:
        out["gripper_command"] = "close"
    return out


def _plan_hf(body: dict[str, Any], url: str, jpeg_bytes: bytes | None = None) -> dict[str, Any]:
    import torch
    from PIL import Image

    err = _ensure_hf_vla()
    if err:
        return {"ok": False, "reason": "openvla_hf_load_failed", "detail": err, "image_url": url}

    try:
        if jpeg_bytes is None:
            raw = _fetch_jpeg(url)
        else:
            raw = jpeg_bytes
        image = Image.open(io.BytesIO(raw)).convert("RGB")
    except (URLError, OSError, TimeoutError, ValueError) as exc:
        return {"ok": False, "reason": "image_fetch_failed", "detail": repr(exc), "image_url": url}

    inst = str(
        body.get("instruction")
        or body.get("language_instruction")
        or body.get("task")
        or os.environ.get("OPENVLA_DEFAULT_INSTRUCTION")
        or "grasp the object"
    ).strip()
    prompt = f"In: What action should the robot take to {inst}?\nOut:"

    processor = _HF_STATE["processor"]
    model = _HF_STATE["model"]
    device = _HF_STATE["device"]
    dtype = _HF_STATE["dtype"]
    unnorm = (os.environ.get("OPENVLA_UNNORM_KEY") or "bridge_orig").strip()

    inputs = processor(prompt, image)
    inputs = inputs.to(device)

    import time as _time

    t0 = _time.perf_counter()
    try:
        with torch.inference_mode():
            action = model.predict_action(**dict(inputs), unnorm_key=unnorm, do_sample=False)
    except Exception as exc:
        return {"ok": False, "reason": "openvla_predict_failed", "detail": repr(exc), "unnorm_key": unnorm}
    predict_s = _time.perf_counter() - t0

    if hasattr(action, "detach"):
        arr = action.detach().float().cpu().numpy().reshape(-1)
    else:
        arr = np.asarray(action, dtype=np.float64).reshape(-1)
    act_list = [float(x) for x in arr.tolist()]
    grasp = _heuristic_grasp_from_action(arr)
    fk_bl = _maybe_openvla_fk_tool_tip_base_link_m(arr)
    if fk_bl is not None:
        grasp = fk_bl

    out_hf: dict[str, Any] = {
        "ok": True,
        "backend": "openvla_hf",
        "hint_it": (
            "Azione 7-DoF da OpenVLA (dataset bridge_orig / unnorm_key). "
            "grasp_display_base_link_m è euristica (OPENVLA_HEURISTIC_*) salvo OPENVLA_ACTION_FK_JOINTS=1."
        ),
        "openvla_action_7dof": act_list,
        "openvla_unnorm_key": unnorm,
        "openvla_hf_model_id": _HF_STATE.get("model_id"),
        "grasp_display_base_link_m": grasp,
        "operators_grasp_points_base_link_m": [grasp, [grasp[0] + 0.01, grasp[1], grasp[2]]],
        "grip_point": {"cx": 320, "cy": 240, "u": 0.5, "v": 0.5},
        "image_url_used": url,
    }
    if fk_bl is not None:
        out_hf["openvla_fk_tool_tip_base_link_m"] = fk_bl
    _attach_openvla_joint_meta(out_hf, act_list)
    _merge_openvla_ui_into_plan(out_hf, arr, None)
    _attach_gripper_and_cameras(body, out_hf, act_list)
    od = out_hf.get("openvla_debug")
    if isinstance(od, dict):
        od["instruction"] = inst
        od["predict_walltime_s"] = round(float(predict_s), 4)
        od["unnorm_key"] = unnorm
    return out_hf


def plan_from_openvla_json(body: dict[str, Any] | None) -> dict[str, Any]:
    body = dict(body or {})
    jpeg_inline, url = _jpeg_from_body(body)
    if not isinstance(url, str):
        return {"ok": False, "reason": "bad_image_url", "hint_it": "image_url deve essere stringa HTTP(S) o embedded://."}

    if _stub_mode():
        if jpeg_inline is None:
            try:
                _fetch_jpeg(url)
            except (URLError, OSError, TimeoutError) as exc:
                return {
                    "ok": False,
                    "reason": "image_fetch_failed",
                    "detail": repr(exc),
                    "image_url": url,
                    "hint_it": "In cloud mode invia jpeg_base64 dalla NX (GO2_GRASP_CLOUD_MODE=1).",
                }
        return _stub_plan_from_image_url(url, body)

    fn, _err = _try_load_plan_fn()
    if fn is not None:
        if jpeg_inline is None:
            try:
                raw = _fetch_jpeg(url)
            except (URLError, OSError, TimeoutError) as exc:
                return {"ok": False, "reason": "image_fetch_failed", "detail": repr(exc), "image_url": url}
        else:
            raw = jpeg_inline
        try:
            out = fn(body, raw)
        except TypeError:
            out = fn(body=body, jpeg_bytes=raw)
        if not isinstance(out, dict):
            return {"ok": False, "reason": "adapter_bad_return", "detail": "callable must return dict"}
        out.setdefault("backend", "openvla_adapter")
        out["image_url_used"] = url
        act = out.get("openvla_action_7dof")
        if isinstance(act, (list, tuple)) and len(act) >= 2:
            try:
                arr_ad = np.asarray([float(x) for x in act], dtype=np.float64)
                _merge_openvla_ui_into_plan(out, arr_ad, out)
                _attach_gripper_and_cameras(body, out, [float(x) for x in act])
            except (TypeError, ValueError):
                pass
        return out

    act_base = _act_server_base()
    if act_base:
        return _plan_act_http(body, url, act_base, jpeg_bytes=jpeg_inline)

    if _hf_mode():
        return _plan_hf(body, url, jpeg_bytes=jpeg_inline)

    return {
        "ok": False,
        "reason": "openvla_not_configured",
        "hint_it": (
            "Imposta OPENVLA_RUNTIME_STUB=1, OPENVLA_ACT_SERVER_URL (server /act), "
            "OPENVLA_USE_HF=1 (+ requirements-openvla.txt), oppure OPENVLA_ADAPTER_CALLABLE."
        ),
        "status": openvla_status(),
    }


def execute_openvla_echo(body: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "ok": True,
        "backend": "openvla_worker",
        "hint_it": "Execute resta sulla catena NX/DDS; qui solo eco richiesta.",
        "request_echo": body or {},
    }

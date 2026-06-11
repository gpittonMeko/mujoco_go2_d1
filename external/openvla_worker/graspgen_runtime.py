"""
GraspGen: inferenza locale o via ZMQ (NVlabs/GraspGen client-server).

Env:
  GO2_GRASP_GEN_ZMQ — es. tcp://127.0.0.1:5556 (se assente → fallback planner)
  GO2_GRASP_GEN_GRIPPER_YML — path config gripper (opzionale, per server)
"""

from __future__ import annotations

import os
import socket
from typing import Any
from urllib.parse import urlparse

import numpy as np


def _zmq_endpoints() -> list[str]:
    raw = (os.environ.get("GO2_GRASP_GEN_ZMQ") or os.environ.get("GRASPGEN_ZMQ_URL") or "").strip()
    out: list[str] = []
    if raw:
        out.append(raw)
    for extra in (
        "tcp://172.17.0.1:5556",
        "tcp://host.docker.internal:5556",
        "tcp://127.0.0.1:5556",
    ):
        if extra not in out:
            out.append(extra)
    return out


def _zmq_endpoint() -> str | None:
    eps = _zmq_endpoints()
    return eps[0] if eps else None


def _zmq_reachable(endpoint: str, timeout_s: float = 1.5) -> bool:
    try:
        if not endpoint.startswith("tcp://"):
            return False
        host_port = endpoint.replace("tcp://", "").split("/")[0]
        host, _, port_s = host_port.rpartition(":")
        port = int(port_s or "5556")
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def graspgen_status() -> dict[str, Any]:
    eps = _zmq_endpoints()
    if not eps:
        return {"available": False, "reason": "GO2_GRASP_GEN_ZMQ unset"}
    for ep in eps:
        if _zmq_reachable(ep):
            return {
                "available": True,
                "endpoint": ep,
                "endpoints_tried": eps,
                "gripper_config": (os.environ.get("GO2_GRASP_GEN_GRIPPER_YML") or "").strip() or None,
            }
    return {
        "available": False,
        "endpoint": eps[0],
        "endpoints_tried": eps,
        "gripper_config": (os.environ.get("GO2_GRASP_GEN_GRIPPER_YML") or "").strip() or None,
    }


def _zmq_client():
    from grasp_gen.serving.zmq_client import GraspGenClient

    st = graspgen_status()
    ep = st.get("endpoint") if st.get("available") else (_zmq_endpoint() or "tcp://127.0.0.1:5556")
    parsed = urlparse(ep if "://" in ep else f"tcp://{ep}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5556
    return GraspGenClient(host=host, port=port)


def infer_grasps_pointcloud(pc: np.ndarray, *, num_grasps: int = 120, topk: int = 20) -> tuple[np.ndarray, np.ndarray]:
    client = _zmq_client()
    try:
        grasps, conf = client.infer(
            pc.astype(np.float32),
            num_grasps=num_grasps,
            topk_num_grasps=topk,
        )
        return grasps, conf
    finally:
        client.close()


def infer_from_metric_pc(body: dict[str, Any] | None) -> dict[str, Any]:
    """Inferenza GraspGen su una point cloud **metrica** gia' pronta (camera optical frame, metri).

    Pensato per il "ponte" NX→GraspGen: la NX cattura la nuvola con l'SDK Orbbec (depth a 16 bit
    + intrinseci reali) e la manda qui; il worker fa solo l'inferenza ZMQ e ritorna le pose grezze
    (4x4) + confidenze, **senza** trasformazioni (le applica il client NX in base_link).

    body::
        {"point_cloud_b64": base64(np.save di (N,3) float32),
         "num_grasps": 200, "topk": 100}
    """
    import base64
    import io

    body = dict(body or {})
    st = graspgen_status()
    if not st.get("available"):
        return {"ok": False, "reason": "zmq_unreachable", "graspgen_status": st}
    b64 = body.get("point_cloud_b64") or body.get("metric_point_cloud_b64")
    if not isinstance(b64, str) or not b64.strip():
        return {"ok": False, "reason": "no_point_cloud"}
    try:
        raw = base64.standard_b64decode(b64.strip())
        pc = np.load(io.BytesIO(raw), allow_pickle=False)
    except Exception as exc:
        return {"ok": False, "reason": "pc_decode_failed", "detail": repr(exc)}
    pc = np.asarray(pc, dtype=np.float32)
    if pc.ndim != 2 or pc.shape[1] != 3 or len(pc) < 32:
        return {"ok": False, "reason": "pc_shape_invalid", "shape": list(pc.shape)}
    num = int(body.get("num_grasps") or 200)
    topk = int(body.get("topk") or body.get("topk_num_grasps") or 100)
    try:
        grasps, conf = infer_grasps_pointcloud(pc, num_grasps=num, topk=topk)
    except ImportError as exc:
        return {"ok": False, "reason": f"client_import:{exc!r}"}
    except Exception as exc:
        return {"ok": False, "reason": "infer_failed", "detail": repr(exc)}
    grasps = np.asarray(grasps, dtype=np.float32) if grasps is not None else np.zeros((0, 4, 4), np.float32)
    conf = np.asarray(conf, dtype=np.float32).reshape(-1) if conf is not None else np.zeros((0,), np.float32)
    if len(grasps) == 0:
        return {"ok": False, "reason": "no_grasps", "num_points": int(len(pc))}
    order = np.argsort(-conf)
    grasps = grasps[order]
    conf = conf[order]
    return {
        "ok": True,
        "backend": "graspgen",
        "num_points": int(len(pc)),
        "num_grasps": int(len(grasps)),
        "grasps_4x4": grasps.tolist(),
        "confidences": [round(float(x), 4) for x in conf.tolist()],
        "gripper": st.get("gripper_config") or "robotiq_2f_140",
    }


def _best_grasp_base_xyz(grasp_4x4: np.ndarray) -> list[float]:
    """Punto presa approssimato: origine del frame grasp (camera frame → euristica base)."""
    t = grasp_4x4[:3, 3]
    # Trasformazione camera→base_link: stessa euristica del planner (offset mount + profondità)
    sx = float(os.environ.get("GO2_BOX_TVEC_SIGN_X", "1"))
    sy = float(os.environ.get("GO2_BOX_TVEC_SIGN_Y", "1"))
    sz = float(os.environ.get("GO2_BOX_TVEC_SIGN_Z", "1"))
    cam = [float(t[0]) * sx, float(t[1]) * sy, float(t[2]) * sz]
    off_x = float(os.environ.get("GO2_GRASP_GEN_BASE_OFFSET_X", "0.38"))
    off_y = float(os.environ.get("GO2_GRASP_GEN_BASE_OFFSET_Y", "0.0"))
    off_z = float(os.environ.get("GO2_GRASP_GEN_BASE_OFFSET_Z", "0.12"))
    return [round(cam[0] + off_x, 4), round(cam[1] + off_y, 4), round(cam[2] + off_z, 4)]


def plan_from_graspgen_json(body: dict[str, Any] | None) -> dict[str, Any]:
    from planner_runtime import plan_from_http_json

    body = dict(body or {})
    st = graspgen_status()
    if not st.get("available"):
        out = plan_from_http_json(body)
        out["graspgen_fallback"] = "planner"
        out["graspgen_reason"] = "zmq_unreachable"
        return out

    try:
        from planner_runtime import _decode_bgr, _ensure_scripts_path
        import base64

        _ensure_scripts_path()
        from box_object_detector import detect_box_object
        from rgbd_pointcloud import point_cloud_from_rgbd

        b64 = body.get("jpeg_base64") or body.get("image_jpeg_b64")
        if not isinstance(b64, str) or not b64.strip():
            return plan_from_http_json(body)
        frame = _decode_bgr(base64.standard_b64decode(b64.strip()))
        depth_b64 = body.get("depth_jpeg_b64")
        depth_bytes = base64.standard_b64decode(depth_b64.strip()) if isinstance(depth_b64, str) and depth_b64.strip() else None
        scale = body.get("depth_scale_m_per_unit")
        try:
            scale_f = float(scale) if scale is not None else None
        except (TypeError, ValueError):
            scale_f = None
        instruction = str(body.get("instruction") or body.get("task") or "").strip()
        obj = body.get("object_detection")
        if not isinstance(obj, dict) or not obj.get("ok"):
            obj = detect_box_object(frame)
            if instruction and isinstance(obj, dict):
                obj["instruction"] = instruction
        pc, pc_meta = point_cloud_from_rgbd(frame, depth_bytes, obj, depth_scale_m_per_unit=scale_f)
        if not pc_meta.get("ok"):
            out = plan_from_http_json({**body, "object_detection": obj})
            out["graspgen_fallback"] = "planner"
            out["graspgen_reason"] = pc_meta.get("reason", "insufficient_points")
            return out
        grasps, conf = infer_grasps_pointcloud(pc)
        if len(grasps) == 0:
            out = plan_from_http_json({**body, "object_detection": obj})
            out["graspgen_fallback"] = "planner"
            out["graspgen_reason"] = "no_grasps"
            return out
        best_i = int(np.argmax(conf))
        g4 = grasps[best_i]
        xyz = _best_grasp_base_xyz(g4)
        out = plan_from_http_json({**body, "object_detection": obj})
        out["backend"] = "graspgen"
        out["graspgen_confidence"] = float(conf[best_i])
        out["graspgen_num_candidates"] = int(len(grasps))
        out["graspgen_point_cloud_points"] = int(pc_meta.get("num_points") or 0)
        out["grasp_display_base_link_m"] = xyz
        out["translation"] = xyz
        out["operators_grasp_points_base_link_m"] = [xyz]
        obj_out = dict(out.get("object_detection") or obj)
        obj_out["backend"] = "graspgen"
        out["object_detection"] = obj_out
        if out.get("target"):
            t = dict(out["target"])
            t["source"] = "graspgen"
            t["base_xyz_m"] = xyz
            t["ok"] = True
            out["target"] = t
        return out
    except ImportError as exc:
        out = plan_from_http_json(body)
        out["graspgen_fallback"] = "planner"
        out["graspgen_reason"] = f"import:{exc!r}"
        return out
    except Exception as exc:
        out = plan_from_http_json(body)
        out["graspgen_fallback"] = "planner"
        out["graspgen_reason"] = repr(exc)
        return out

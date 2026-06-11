"""Serve mesh per viewer Three.js (stesso contratto del monolite ``diagnostics_dashboard``)."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from flask import abort, send_from_directory
from werkzeug.utils import secure_filename

from go2_dashboard.paths import PROJECT_ROOT

GO2_SCENE_ASSETS_DIR = PROJECT_ROOT / "unitree_mujoco" / "unitree_robots" / "go2_d1" / "assets"
D1_SCENE_MESH_DIR = PROJECT_ROOT / "unitree_mujoco" / "unitree_robots" / "go2_d1" / "d1_550_description" / "meshes"


def scene_mesh_manifest() -> dict[str, list[str]]:
    go2: list[str] = []
    d1: list[str] = []
    if GO2_SCENE_ASSETS_DIR.is_dir():
        go2 = sorted(p.name for p in GO2_SCENE_ASSETS_DIR.glob("*.obj"))
    if D1_SCENE_MESH_DIR.is_dir():
        d1 = sorted(p.name for p in D1_SCENE_MESH_DIR.glob("*.STL"))
    return {"go2_obj": go2, "d1_stl": d1}


def _rpy_to_quat_xyzw_ros(roll: float, pitch: float, yaw: float) -> list[float]:
    cr, sr = math.cos(roll * 0.5), math.sin(roll * 0.5)
    cp, sp = math.cos(pitch * 0.5), math.sin(pitch * 0.5)
    cy, sy = math.cos(yaw * 0.5), math.sin(yaw * 0.5)
    qw = cr * cp * cy + sr * sp * sy
    qx = sr * cp * cy - cr * sp * sy
    qy = cr * sp * cy + sr * cp * sy
    qz = cr * cp * sy - sr * sp * cy
    return [qx, qy, qz, qw]


def d1_mesh_visual_offsets_m() -> list[dict[str, Any]]:
    """Offset visual mesh per link D1 (base + Empty_Link1..6), da URDF se presente."""
    ident = {"pos_m": [0.0, 0.0, 0.0], "quat_xyzw": [0.0, 0.0, 0.0, 1.0]}
    out: list[dict[str, Any]] = [dict(ident) for _ in range(7)]
    cands: list[Path] = []
    envp = os.environ.get("GO2_D1_URDF_PATH", "").strip()
    if envp:
        cands.append(Path(envp))
    cands.extend(
        [
            PROJECT_ROOT / "d1_550_description" / "urdf" / "d1_550_description.urdf",
            PROJECT_ROOT
            / "unitree_mujoco"
            / "unitree_robots"
            / "go2_d1"
            / "d1_550_description"
            / "urdf"
            / "d1_550_description.urdf",
        ]
    )
    path = next((p for p in cands if str(p) and p.is_file()), None)
    if path is None:
        return out
    try:
        import xml.etree.ElementTree as ET

        tree = ET.parse(str(path))
        root = tree.getroot()
        for link in root.findall("link"):
            vis = link.find("visual")
            if vis is None:
                continue
            geom_el = vis.find("geometry")
            mesh_el = geom_el.find("mesh") if geom_el is not None else None
            fn = ""
            if mesh_el is not None:
                fn = (mesh_el.get("filename") or mesh_el.get("uri") or "").replace("\\", "/")
            fnl = fn.lower()
            idx: int | None = None
            if "base_link" in fnl:
                idx = 0
            else:
                for li in range(1, 7):
                    if f"empty_link{li}" in fnl or f"link{li}.stl" in fnl:
                        idx = li
                        break
            if idx is None:
                continue
            orig = vis.find("origin")
            xyz = [0.0, 0.0, 0.0]
            rpy = [0.0, 0.0, 0.0]
            if orig is not None:
                xs = orig.get("xyz", "0 0 0").split()
                rs = orig.get("rpy", "0 0 0").split()
                if len(xs) >= 3:
                    xyz = [float(xs[0]), float(xs[1]), float(xs[2])]
                if len(rs) >= 3:
                    rpy = [float(rs[0]), float(rs[1]), float(rs[2])]
            q = _rpy_to_quat_xyzw_ros(rpy[0], rpy[1], rpy[2])
            out[idx] = {
                "pos_m": [round(float(xyz[i]), 6) for i in range(3)],
                "quat_xyzw": [round(float(q[i]), 6) for i in range(4)],
            }
    except Exception:
        return [dict(ident) for _ in range(7)]
    return out


def send_scene_mesh_file(kind: str, filename: str) -> Any:
    if kind == "go2":
        if not filename.lower().endswith(".obj"):
            abort(404)
        base_dir = GO2_SCENE_ASSETS_DIR
    elif kind == "d1":
        if not filename.lower().endswith(".stl"):
            abort(404)
        base_dir = D1_SCENE_MESH_DIR
    else:
        abort(404)
    safe = secure_filename(filename)
    if not safe or safe != filename:
        abort(404)
    path = base_dir / safe
    if not path.is_file():
        abort(404)
    mimetype = "model/stl" if kind == "d1" else "text/plain"
    return send_from_directory(str(base_dir), safe, mimetype=mimetype)

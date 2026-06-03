#!/usr/bin/env python3
"""
Scarica URDF + mesh D1-550 dal repository community (stessi file del pacchetto d1_550_description):

  https://github.com/JeewanthaSadaruwan/unitree-D1-550-Robot-ARM

Salva in::
  unitree_mujoco/unitree_robots/go2_d1/d1_550_description/urdf/d1_550_description.urdf
  unitree_mujoco/unitree_robots/go2_d1/d1_550_description/meshes/*.STL

Esegui dalla root del repo::
  python scripts/fetch_d1_550_from_jeewantha_github.py

Richiede solo ``urllib`` (stdlib). Nessuna chiave GitHub per file pubblici.
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BRANCH = os.environ.get("D1_550_GITHUB_BRANCH", "main")
GITHUB_USER_REPO = "JeewanthaSadaruwan/unitree-D1-550-Robot-ARM"
BASE = f"https://raw.githubusercontent.com/{GITHUB_USER_REPO}/{BRANCH}/d1_550_description"

FILES_URDF = [
    "urdf/d1_550_description.urdf",
    "urdf/d1_550_description.urdf.xacro",
    "urdf/d1_550_description.csv",
]

FILES_MESH = [
    "meshes/base_link.STL",
    "meshes/Empty_Link1.STL",
    "meshes/Empty_Link2.STL",
    "meshes/Empty_Link3.STL",
    "meshes/Empty_Link4.STL",
    "meshes/Empty_Link5.STL",
    "meshes/Empty_Link6.STL",
]


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "mujoco_go2_d1-fetch-script"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    if len(data) < 50 and dest.suffix.lower() == ".stl":
        raise RuntimeError(f"Scaricamento sospetto (troppo piccolo): {url}")
    dest.write_bytes(data)


def main() -> int:
    n_ok = 0
    for rel in FILES_URDF:
        url = f"{BASE}/{rel}"
        dest = REPO_ROOT / "unitree_mujoco" / "unitree_robots" / "go2_d1" / "d1_550_description" / rel.replace(
            "\\", "/"
        )
        try:
            _download(url, dest)
            print("ok", rel, "->", dest.relative_to(REPO_ROOT))
            n_ok += 1
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            print("ERRORE", rel, exc, file=sys.stderr)
            return 1
    for rel in FILES_MESH:
        url = f"{BASE}/{rel}"
        dest = REPO_ROOT / "unitree_mujoco" / "unitree_robots" / "go2_d1" / "d1_550_description" / rel.replace(
            "\\", "/"
        )
        try:
            _download(url, dest)
            print("ok", rel, "->", dest.relative_to(REPO_ROOT), f"({dest.stat().st_size} B)")
            n_ok += 1
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            print("ERRORE", rel, exc, file=sys.stderr)
            return 1
    print("fatto:", n_ok, "file da", BASE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Copia gli STL del braccio D1 dalla cartella ``d1_550_description/meshes`` del pacchetto Unitree
(o da un clone community con gli stessi file, p.es.
``https://github.com/JeewanthaSadaruwan/unitree-D1-550-Robot-ARM/tree/main/d1_550_description/meshes``)
verso il path usato da ``go2_d1_d1mesh.xml`` nel repo.

Esempio (Windows / Linux):

    python scripts/sync_d1_meshes_from_package.py "C:/path/to/d1_550_description/meshes"
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEST = REPO_ROOT / "unitree_mujoco" / "unitree_robots" / "go2_d1" / "d1_550_description" / "meshes"

FILES = ["base_link.STL"] + [f"Empty_Link{i}.STL" for i in range(1, 7)]


def main() -> int:
    ap = argparse.ArgumentParser(description="Copia STL D1 nel repo (stessi file referenziati dal MJCF).")
    ap.add_argument(
        "source_meshes_dir",
        type=Path,
        help="Directory sorgente …/d1_550_description/meshes",
    )
    args = ap.parse_args()
    src_dir = args.source_meshes_dir
    if not src_dir.is_dir():
        print("ERRORE: cartella inesistente:", src_dir)
        return 1
    DEST.mkdir(parents=True, exist_ok=True)
    n = 0
    for name in FILES:
        s = src_dir / name
        if not s.is_file():
            print("manca:", s)
            continue
        shutil.copy2(s, DEST / name)
        print("ok", name)
        n += 1
    print("copiati", n, "file in", DEST)
    return 0 if n == len(FILES) else 2


if __name__ == "__main__":
    raise SystemExit(main())

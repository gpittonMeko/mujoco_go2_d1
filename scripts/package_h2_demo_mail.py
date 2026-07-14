#!/usr/bin/env python3
"""Create deliverable folder + zip: APRI_DOCUMENTAZIONE.html + all .md"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "docs" / "h2_demo_mail_package"
DELIVER = REPO / "docs" / "H2_Demo_Documentazione"
OUT_ZIP = REPO / "docs" / "H2_Demo_Documentazione.zip"
HTML_SRC = REPO / "docs" / "H2_Demo_Documentazione.html"
HTML_OPEN = "APRI_DOCUMENTAZIONE.html"
MD_DIR = "markdown"
LEGGIMI_TXT = """H2 Demo — Documentazione
=========================

APRI NEL BROWSER (doppio click):

    APRI_DOCUMENTAZIONE.html

Ordine consigliato:
  1) 01_LA_DEMO.md        — cosa fa la demo e come lanciarla
  2) 02_ACCENSIONE_...    — accensione, password, mani Revo sul .162
  3) 03_FILE_SUL_ROBOT.md — dove sta il codice

Cartella markdown/ = sorgenti per aggiornamenti.
"""


def _build_html() -> int:
    return subprocess.call([sys.executable, str(REPO / "scripts" / "package_h2_demo_html.py")])


def main() -> int:
    if not PKG.is_dir():
        print(f"Missing package dir: {PKG}", flush=True)
        return 1

    print("Building HTML...")
    if _build_html() != 0:
        return 1
    if not HTML_SRC.is_file():
        print(f"Missing {HTML_SRC}", flush=True)
        return 1

    md_files = sorted(PKG.glob("*.md"), key=lambda p: p.name)
    if not md_files:
        print("No .md files in package", flush=True)
        return 1

    if DELIVER.exists():
        shutil.rmtree(DELIVER)
    DELIVER.mkdir(parents=True)
    (DELIVER / MD_DIR).mkdir()

    shutil.copy2(HTML_SRC, DELIVER / HTML_OPEN)
    (DELIVER / "LEGGIMI.txt").write_text(LEGGIMI_TXT, encoding="utf-8")
    for f in md_files:
        shutil.copy2(f, DELIVER / MD_DIR / f.name)

    # Also keep canonical html next to zip for quick open from repo
    shutil.copy2(HTML_SRC, REPO / "docs" / HTML_OPEN)

    with zipfile.ZipFile(OUT_ZIP, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DELIVER.rglob("*")):
            if path.is_file():
                arc = path.relative_to(DELIVER.parent).as_posix()
                zf.write(path, arc)
                print(f"  + {arc}")

    print(f"\nCartella da inviare: {DELIVER}")
    print(f"ZIP allegato mail:   {OUT_ZIP} ({OUT_ZIP.stat().st_size // 1024} KB)")
    print(f"\n>>> Chi riceve: estrae lo ZIP e apre {HTML_OPEN}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

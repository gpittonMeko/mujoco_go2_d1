#!/usr/bin/env python3
r"""Applica modifiche compatibili a ``openvla/vla-scripts/deploy.py`` (upstream GitHub).

Problema: il worker ``mujoco_go2_d1`` invia JSON standard (immagine = liste annidate, niente
``json_numpy`` lato client). Il ``deploy.py`` upstream assume spesso ``np.ndarray`` e ritorna
``JSONResponse(action)`` con tensor/numpy non serializzabili in JSON puro.

Questo script (idempotente, marker ``# go2_json_compat_patch``):

1. Aggiunge ``import numpy as np`` se manca.
2. Dopo ``unnorm_key = ...`` converte ``image`` in ``np.ndarray`` uint8 se non lo è già.
3. Nel ramo ``else`` di ``double_encode``, converte ``action`` in ``list[float]`` e ritorna
   ``JSONResponse({"action": plain_action})``.

Uso (sulla RTX, dopo ``git clone https://github.com/openvla/openvla``):

  python scripts/patch_openvla_upstream_deploy.py --openvla-root C:/Users/Utente/source/openvla

Opzionale ``--dry-run`` stampa le modifiche senza scrivere. Backup: ``deploy.py.bak`` accanto al file.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


MARKER = "# go2_json_compat_patch"


def _ensure_numpy_import(text: str) -> str:
    if "import numpy as np" in text or "import numpy\n" in text:
        return text
    needle = "import logging\n"
    if needle not in text:
        raise SystemExit("patch: pattern 'import logging\\n' non trovato in deploy.py")
    return text.replace(needle, needle + "import numpy as np\n", 1)


def _insert_image_asarray(lines: list[str]) -> list[str] | None:
    """Inserisce blocco immagine dopo la riga unnorm_key; ritorna nuove linee o None se già presente."""
    out: list[str] = []
    inserted = False
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        if (
            not inserted
            and 'unnorm_key = payload.get("unnorm_key", None)' in line
            and i + 1 < len(lines)
            and "# Run VLA Inference" in lines[i + 1]
        ):
            if any(MARKER in x for x in lines):
                return None
            out.append("\n")
            out.append(f"            {MARKER}: image from JSON may be nested lists\n")
            out.append("            if not isinstance(image, np.ndarray):\n")
            out.append("                image = np.asarray(image, dtype=np.uint8)\n")
            inserted = True
        i += 1
    if not inserted:
        return None
    return out


def _replace_jsonresponse_action(text: str) -> str:
    old = (
        "            if double_encode:\n"
        "                return JSONResponse(json_numpy.dumps(action))\n"
        "            else:\n"
        "                return JSONResponse(action)\n"
    )
    if old not in text:
        if '"action": plain_action' in text or "plain_action" in text:
            return text
        raise SystemExit("patch: blocco return JSONResponse(action) non trovato (deploy.py diverso da main?)")
    new = (
        "            if double_encode:\n"
        "                return JSONResponse(json_numpy.dumps(action))\n"
        "            else:\n"
        f"                {MARKER}: plain list for HTTP JSON clients (go2 worker)\n"
        "                def _action_to_plain(x: Any) -> list[float]:\n"
        "                    if hasattr(x, \"detach\"):\n"
        "                        x = x.detach().float().cpu().numpy()\n"
        "                    arr = np.asarray(x, dtype=np.float64).reshape(-1)\n"
        "                    return [float(v) for v in arr.tolist()]\n"
        "\n"
        "                plain_action = _action_to_plain(action)\n"
        '                return JSONResponse({"action": plain_action})\n'
    )
    return text.replace(old, new, 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--openvla-root",
        type=Path,
        required=True,
        help="Root del clone openvla (contiene vla-scripts/deploy.py)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Non scrivere file")
    args = ap.parse_args()
    root: Path = args.openvla_root.expanduser().resolve()
    target = root / "vla-scripts" / "deploy.py"
    if not target.is_file():
        print("ERR: manca", target, file=sys.stderr)
        return 1

    original = target.read_text(encoding="utf-8")
    if MARKER in original:
        print("OK: deploy.py già patchato (marker presente):", target)
        return 0

    text = original
    text = _ensure_numpy_import(text)
    lines = text.splitlines(keepends=True)
    img_lines = _insert_image_asarray(lines)
    if img_lines is None:
        if MARKER in original:
            pass
        else:
            raise SystemExit("patch: inserimento immagine fallito (pattern unnorm_key / Run VLA Inference)")
        text = original
    else:
        text = "".join(img_lines)

    text = _replace_jsonresponse_action(text)

    if args.dry_run:
        print("--- dry-run: prime 40 righe modificate (diff concettuale) ---")
        o_lines = original.splitlines()
        n_lines = text.splitlines()
        for i, (a, b) in enumerate(zip(o_lines, n_lines), 1):
            if a != b:
                print(f"{i}: - {a}")
                print(f"{i}: + {b}")
        print("... (dry-run non salva)")
        return 0

    bak = target.with_suffix(".py.bak")
    shutil.copy2(target, bak)
    target.write_text(text, encoding="utf-8", newline="\n")
    print("OK: patch applicata:", target)
    print("OK: backup:", bak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

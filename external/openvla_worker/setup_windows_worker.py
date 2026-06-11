"""Setup worker su Windows: pip con tqdm (una barra per pacchetto in requirements.txt)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQ = ROOT / "requirements.txt"
REQ_HF = ROOT / "requirements-openvla.txt"


def main() -> None:
    try:
        from tqdm import tqdm
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "tqdm"], cwd=str(ROOT))
        from tqdm import tqdm

    specs: list[str] = []
    if REQ.is_file():
        for line in REQ.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                specs.append(s)

    for spec in tqdm(specs, desc="pip install (requirements)", unit="pkg", dynamic_ncols=True):
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", spec],
            cwd=str(ROOT),
        )
    tqdm.write("OK: dipendenze installate.")

    if os.environ.get("OPENVLA_INSTALL_HF_DEPS", "").lower() in {"1", "true", "yes", "on"} and REQ_HF.is_file():
        hf_specs: list[str] = []
        for line in REQ_HF.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                hf_specs.append(s)
        for spec in tqdm(hf_specs, desc="pip install (openvla HF)", unit="pkg", dynamic_ncols=True):
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", spec],
                cwd=str(ROOT),
            )
        tqdm.write("OK: dipendenze Hugging Face OpenVLA installate.")


if __name__ == "__main__":
    main()

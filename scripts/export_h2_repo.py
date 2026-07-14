#!/usr/bin/env python3
"""Export H2 demo into standalone repo unitree-h2-testing (sibling folder)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
DST = SRC.parent / "unitree-h2-testing"
REPO_NAME = "unitree-h2-testing"

SCRIPT_NAMES = [
    "h2_common.py",
    "h2_wav_util.py",
    "h2_hand_util.py",
    "h2_demo_casoria.py",
    "h2_demo_pellegrino.py",
    "h2_left_arm_raise.py",
    "h2_right_arm_hand.py",
    "h2_recover_stand.py",
    "h2_probe_lowstate.py",
    "h2_probe_hand.py",
    "h2_probe_hand_dds.py",
    "h2_probe_jetson_env.py",
    "h2_scan_hand_topics.py",
    "h2_test_tts.py",
    "h2_play_wav.py",
    "h2_verify_sdk.py",
    "h2_verify_arm_sdk.py",
    "h2_arm_diag.py",
    "h2_arm_nudge.py",
    "h2_arm_wave_test.py",
    "h2_arm_emergency_stop.py",
    "h2_micro_right_arm.py",
    "h2_smoke_remote.py",
    "h2_start_brainco_pc2.py",
    "h2_pc2_readonly_check.py",
    "h2_bundle_offline_deps.py",
    "deploy_h2_demo_to_jetson.py",
    "package_h2_demo_mail.py",
    "package_h2_demo_html.py",
    "generate_pellegrino_tts_wav.py",
    "h2_install_sdk_jetson.sh",
]

AUDIO = ["pellegrino_tts.wav", "cyberpunk_meme.wav"]

GITIGNORE = """# Python
__pycache__/
*.py[cod]
.venv/
venv/

# Build / cache
.cache/
*.egg-info/

# Generated docs (rigenerare con package_h2_demo_mail.py)
docs/H2_Demo_Documentazione/
docs/H2_Demo_Documentazione.html
docs/H2_Demo_Documentazione.zip
docs/APRI_DOCUMENTAZIONE.html

# Audio temp
data/audio/_tts_parts/
data/audio/concat_list.txt

# IDE
.idea/
.vscode/
"""

REQUIREMENTS = """# PC lab — smoke test, deploy, packaging
paramiko>=3.0
numpy>=1.24
markdown>=3.5

# Opzionale: rigenerare pellegrino_tts.wav
edge-tts>=6.1
"""

README = f"""# {REPO_NAME}

Script e documentazione per **test e demo su Unitree H2** (TTS + audio + braccio sinistro + mano BrainCo Revo)  
sulla Jetson Thor, LAN `192.168.123.0/24`.

## Documentazione

1. Genera il pacchetto:
   ```powershell
   python scripts/package_h2_demo_mail.py
   ```
2. Condividi `docs/H2_Demo_Documentazione.zip` oppure apri `APRI_DOCUMENTAZIONE.html`.

Sorgenti: `docs/h2_demo_mail_package/`.

## Setup

```powershell
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
```

## Demo rapida

```powershell
python scripts/h2_start_brainco_pc2.py
python scripts/h2_smoke_remote.py hand-dds
python scripts/h2_smoke_remote.py demo --confirm-arm
```

Robot in **Regular Mode 1**, protection frame, piedi a terra — vedi documentazione.

## Deploy Thor (`.163`)

```powershell
python scripts/deploy_h2_demo_to_jetson.py
```

Installa in `/home/unitree/h2_demo`.

## Contenuto repo

| Percorso | Contenuto |
|----------|-----------|
| `scripts/h2_*.py` | Demo, smoke test, deploy, mani |
| `data/audio/` | WAV demo |
| `unitree_sdk2_python/` | SDK Unitree |
| `docs/` | Documentazione operatore |

## Rete

| IP | Ruolo | SSH |
|----|-------|-----|
| `.161` | Locomozione | non accessibile |
| `.162` | Mani Revo | `unitree` / `Unitree#24226` |
| `.163` | Jetson Thor | `unitree` / `123` |

## Note

- SDK: [unitree_sdk2_python](https://github.com/unitreerobotics/unitree_sdk2_python)
- `brainco_hand_service` sul PC2 Unitree — non incluso qui
"""


def _patch_doc_for_export(text: str) -> str:
    return (
        text.replace("mujoco_go2_d1", REPO_NAME)
        .replace("Mujoco Cane", REPO_NAME)
        .replace("mujoco-cane", REPO_NAME)
    )


def _copy_file(src: Path, dst: Path, *, patch: bool = False) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if patch:
        dst.write_text(_patch_doc_for_export(src.read_text(encoding="utf-8")), encoding="utf-8")
    else:
        shutil.copy2(src, dst)
    print(f"  {src.relative_to(SRC)}")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Export standalone H2 demo repo")
    parser.add_argument("--dest", type=Path, default=DST, help="Destination folder")
    parser.add_argument("--with-sdk", action="store_true", default=True)
    parser.add_argument("--no-sdk", action="store_true", help="Skip unitree_sdk2_python copy")
    parser.add_argument("--git-init", action="store_true", help="Run git init in dest")
    args = parser.parse_args()

    dest: Path = args.dest.resolve()
    if dest.exists():
        print(f"Updating {dest}")
    else:
        print(f"Creating {dest}")
        dest.mkdir(parents=True)

    print("Scripts...")
    for name in SCRIPT_NAMES:
        src = SRC / "scripts" / name
        if not src.is_file():
            print(f"  WARN missing {name}", file=sys.stderr)
            continue
        _copy_file(src, dest / "scripts" / name)

    print("Docs...")
    pkg = SRC / "docs" / "h2_demo_mail_package"
    if pkg.is_dir():
        for f in pkg.glob("*.md"):
            _copy_file(f, dest / "docs" / "h2_demo_mail_package" / f.name, patch=True)
    jetson = SRC / "docs" / "h2_demo_jetson_README.md"
    if jetson.is_file():
        _copy_file(jetson, dest / "docs" / "h2_demo_jetson_README.md", patch=True)

    print("Audio...")
    for name in AUDIO:
        src = SRC / "data" / "audio" / name
        if src.is_file():
            _copy_file(src, dest / "data" / "audio" / name)
        else:
            print(f"  WARN missing {name}", file=sys.stderr)

    copy_sdk = args.with_sdk and not args.no_sdk
    sdk_src = SRC / "unitree_sdk2_python"
    if copy_sdk and sdk_src.is_dir():
        print("SDK (may take a minute)...")
        sdk_dst = dest / "unitree_sdk2_python"
        if sdk_dst.exists():
            shutil.rmtree(sdk_dst)
        shutil.copytree(
            sdk_src,
            sdk_dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
        )
        print(f"  unitree_sdk2_python/ ({len(list(sdk_dst.rglob('*')))} items)")
    elif not sdk_src.is_dir():
        print("  WARN no unitree_sdk2_python in source repo", file=sys.stderr)

    (dest / "README.md").write_text(README, encoding="utf-8")
    (dest / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (dest / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    print("  README.md, .gitignore, requirements.txt")

    print("Building documentation package...")
    subprocess.run([sys.executable, str(dest / "scripts" / "package_h2_demo_mail.py")], check=True, cwd=dest)

    if args.git_init or not (dest / ".git").exists():
        print("git init...")
        subprocess.run(["git", "init"], cwd=dest, check=True)
        print("Repo pronto. Per primo commit: cd", dest, "&& git add . && git commit -m \"...\" ")

    print(f"\nDone: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

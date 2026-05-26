#!/usr/bin/env python3
"""Verifica ambiente RTX per OpenVLA / PyTorch CUDA (da eseguire sul PC con GPU).

Non scarica pesi. Controlla: torch, CUDA, memoria, path OPENVLA_* opzionali,
import opzionale di ``openvla_runtime`` dal worker del repo.

Uso (sul PC RTX, dalla root del repo o con PYTHONPATH):
  python scripts/verify_openvla_rtx_env.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    worker_dir = repo / "external" / "openvla_worker"
    sys.path.insert(0, str(worker_dir))

    ok = True
    print("repo_root:", repo)
    print("OPENVLA_REPO_ROOT:", (os.environ.get("OPENVLA_REPO_ROOT") or "").strip() or "(non impostato)")
    print("OPENVLA_CHECKPOINT:", (os.environ.get("OPENVLA_CHECKPOINT") or "").strip() or "(non impostato)")
    print("OPENVLA_RUNTIME_STUB:", (os.environ.get("OPENVLA_RUNTIME_STUB") or "0").strip())
    use_hf = os.environ.get("OPENVLA_USE_HF", "0").lower() in {"1", "true", "yes", "on"}
    print("OPENVLA_USE_HF:", use_hf)

    cuda_avail = False
    try:
        import torch

        print("torch:", torch.__version__)
        cuda_avail = torch.cuda.is_available()
        print("torch.cuda.is_available:", cuda_avail)
        if cuda_avail:
            print("torch.cuda.get_device_name(0):", torch.cuda.get_device_name(0))
            print("torch.cuda.mem_get_info (free,total) bytes:", torch.cuda.mem_get_info(0))
    except Exception as exc:
        print("FAIL torch:", repr(exc))
        ok = False

    if use_hf:
        try:
            import transformers

            print("transformers:", transformers.__version__)
        except Exception as exc:
            print("FAIL transformers (necessario se OPENVLA_USE_HF=1):", repr(exc))
            ok = False
        if ok and not cuda_avail:
            print("FAIL: OPENVLA_USE_HF richiede CUDA")
            ok = False

    ck = (os.environ.get("OPENVLA_CHECKPOINT") or "").strip()
    if ck:
        p = Path(ck)
        print("checkpoint exists:", p.exists(), "->", p)
        if not p.exists():
            ok = False

    root = (os.environ.get("OPENVLA_REPO_ROOT") or "").strip()
    if root:
        pr = Path(root)
        print("OPENVLA_REPO_ROOT exists:", pr.is_dir(), "->", pr)
        if not pr.is_dir():
            ok = False

    try:
        from openvla_runtime import openvla_status  # type: ignore

        st = openvla_status()
        print("openvla_status:", json_dumps_safe(st))
    except Exception as exc:
        print("openvla_runtime (opzionale):", repr(exc))

    if ok:
        print("VERIFY_OPENVLA_RTX_ENV_OK")
        return 0
    print("VERIFY_OPENVLA_RTX_ENV_FAIL", file=sys.stderr)
    return 1


def json_dumps_safe(obj: object) -> str:
    try:
        return json.dumps(obj, indent=2, default=str)[:4000]
    except Exception:
        return str(obj)[:2000]


if __name__ == "__main__":
    raise SystemExit(main())

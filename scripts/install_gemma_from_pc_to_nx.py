#!/usr/bin/env python3
"""Install Gemma 2 1B IT on Jetson NX when la NX non ha Internet verso GitHub/HF.

Dal PC in LAN verso la NX:
  python scripts/install_gemma_from_pc_to_nx.py

Copia llama.cpp (sorgenti) + GGUF Q4_K_M, compila sulla NX (CUDA sm_87), avvia llama-server.
Hermes :5052 usa già GO2_HERMES_OPENAI_BASE_URL=http://127.0.0.1:8080/v1 e GO2_HERMES_MODEL=gemma-2-1b-it.
"""
from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import paramiko

REPO = Path(__file__).resolve().parent.parent
REMOTE_BASE = (os.environ.get("GO2_DEPLOY_REMOTE_BASE") or "/home/unitree/go2_visual_dashboard").strip()
CACHE = REPO / ".cache" / "gemma_nx"
LLAMA_ZIP_URL = os.environ.get(
    "GO2_LLAMA_ZIP_URL",
    "https://github.com/ggerganov/llama.cpp/archive/refs/heads/master.zip",
)
GGUF_URL = os.environ.get(
    "GO2_GEMMA_GGUF_URL",
    "https://huggingface.co/lmstudio-community/gemma-2-2b-it-GGUF/resolve/main/gemma-2-2b-it-Q4_K_M.gguf",
)
MODEL_FILE = os.environ.get("GO2_GEMMA_MODEL_FILE", "gemma-2-2b-it-Q4_K_M.gguf")
MODEL_ALIAS = os.environ.get("GO2_HERMES_MODEL", "gemma-2-2b-it")


def nx_host() -> str:
    return (os.environ.get("GO2_NX_HOST") or "192.168.123.18").strip() or "192.168.123.18"


def nx_user() -> str:
    return (os.environ.get("GO2_NX_USER") or "unitree").strip() or "unitree"


def nx_password() -> str:
    return os.environ.get("GO2_NX_PASSWORD") or "123"


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 1_000_000:
        print(f"skip download (cache) {dest.name} ({dest.stat().st_size} B)")
        return
    print(f"download {url} -> {dest}")
    headers = {"User-Agent": "go2-dashboard-gemma-install/1.0"}
    hf_token = (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN") or "").strip()
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=600) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


def _ensure_llama_sources() -> Path:
    zip_path = CACHE / "llama.cpp-master.zip"
    src_root = CACHE / "llama.cpp-master"
    _download(LLAMA_ZIP_URL, zip_path)
    if not src_root.is_dir():
        print("extract llama.cpp …")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(CACHE)
    if not src_root.is_dir():
        raise SystemExit(f"missing extracted {src_root}")
    return src_root


def _ensure_gguf() -> Path:
    local = (os.environ.get("GO2_GEMMA_GGUF_LOCAL") or "").strip()
    if local:
        p = Path(local)
        if not p.is_file():
            raise SystemExit(f"GO2_GEMMA_GGUF_LOCAL not found: {p}")
        print(f"use local GGUF {p} ({p.stat().st_size} B)")
        return p
    gguf = CACHE / MODEL_FILE
    try:
        _download(GGUF_URL, gguf)
    except Exception as exc:
        print(f"HF download failed: {exc}")
        try:
            from huggingface_hub import hf_hub_download

            token = (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN") or "").strip() or None
            got = hf_hub_download(
                repo_id="lmstudio-community/gemma-2-2b-it-GGUF",
                filename=MODEL_FILE,
                local_dir=str(CACHE),
                token=token,
            )
            gguf = Path(got)
            print(f"hf_hub_download OK -> {gguf}")
        except Exception as exc2:
            raise SystemExit(
                "Impossibile scaricare Gemma 2 1B IT GGUF. "
                "Accetta la licenza su Hugging Face, poi:\n"
                "  set HF_TOKEN=hf_...\n"
                "  set GO2_GEMMA_GGUF_LOCAL=C:\\path\\gemma-2-2b-it-Q4_K_M.gguf\n"
                "  python scripts/install_gemma_from_pc_to_nx.py"
            ) from exc2
    return gguf


def _sftp_put_tree(sftp: paramiko.SFTPClient, local: Path, remote: str) -> None:
    if local.is_file():
        parent = remote.rsplit("/", 1)[0]
        try:
            sftp.stat(parent)
        except OSError:
            _sftp_mkdirs(sftp, parent)
        sftp.put(str(local), remote)
        return
    _sftp_mkdirs(sftp, remote)
    for item in local.iterdir():
        if item.name in {".git", "build", "__pycache__"}:
            continue
        _sftp_put_tree(sftp, item, f"{remote}/{item.name}")


def _sftp_mkdirs(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    if not remote_dir or remote_dir == "/":
        return
    parts = [p for p in remote_dir.split("/") if p]
    cur = "/" if remote_dir.startswith("/") else ""
    for part in parts:
        cur = f"{cur}/{part}" if cur else part
        try:
            sftp.stat(cur)
        except OSError:
            try:
                sftp.mkdir(cur)
            except OSError:
                pass


def upload_llama_sources(ssh: paramiko.SSHClient, sftp: paramiko.SFTPClient, llama_src: Path) -> None:
    print("[llama pc->nx] upload llama.cpp sources …")
    with tempfile.TemporaryDirectory() as td:
        tgz = Path(td) / "llama.cpp.tgz"
        with tarfile.open(tgz, "w:gz") as tf:
            tf.add(llama_src, arcname="llama.cpp")
        _sftp_mkdirs(sftp, "/home/unitree")
        sftp.put(str(tgz), "/home/unitree/llama.cpp-upload.tgz")
    _, stdout, stderr = ssh.exec_command(
        "rm -rf /home/unitree/llama.cpp && mkdir -p /home/unitree && "
        "tar -xzf /home/unitree/llama.cpp-upload.tgz -C /home/unitree && "
        "rm -f /home/unitree/llama.cpp-upload.tgz && echo LLAMA_SOURCES_OK",
        timeout=120,
    )
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if out:
        print(out)
    if "LLAMA_SOURCES_OK" not in out:
        raise RuntimeError(f"extract failed: {err[-500:]}")
    time.sleep(1)


def _ensure_cmake_wheel() -> Path:
    wheel_dir = CACHE / "wheels"
    wheels = sorted(wheel_dir.glob("cmake-*-manylinux2014_aarch64*.whl"))
    if wheels:
        return wheels[-1]
    wheel_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "cmake",
            "-d",
            str(wheel_dir),
            "--platform",
            "manylinux2014_aarch64",
            "--python-version",
            "38",
            "--only-binary=:all:",
        ],
        check=True,
    )
    wheels = sorted(wheel_dir.glob("cmake-*-manylinux2014_aarch64*.whl"))
    if not wheels:
        raise SystemExit("cmake wheel aarch64 not found")
    return wheels[-1]


def install_cmake_on_nx(ssh: paramiko.SSHClient, sftp: paramiko.SFTPClient) -> None:
    check = 'export PATH="$HOME/.local/bin:$PATH"; cmake --version 2>/dev/null | awk \'/version/{print $3; exit}\''
    _, so, _ = ssh.exec_command(check, timeout=20)
    ver = so.read().decode(errors="replace").strip()
    if ver:
        parts = [int(x) for x in ver.split(".")[:3]]
        if tuple(parts) >= (3, 18, 0):
            print(f"[llama pc->nx] cmake {ver} OK")
            return
    wheel = _ensure_cmake_wheel()
    remote_wheel = f"/tmp/{wheel.name}"
    print(f"[llama pc->nx] upload cmake wheel {wheel.name} …")
    sftp.put(str(wheel), remote_wheel)
    _, so, se = ssh.exec_command(
        f"python3 -m pip install --user --force-reinstall {remote_wheel} 2>&1 | tail -8; "
        'export PATH="$HOME/.local/bin:$PATH"; cmake --version | head -1',
        timeout=180,
    )
    print(so.read().decode(errors="replace").strip())
    err = se.read().decode(errors="replace").strip()
    if err:
        print("cmake pip stderr:", err[-400:])


def build_llama_server_on_nx(ssh: paramiko.SSHClient) -> str:
    use_cuda = os.environ.get("GO2_LLAMA_CUDA", "0").strip().lower() in {"1", "true", "yes", "on"}
    build_script = """set -e
export PATH="$HOME/.local/bin:/usr/local/cuda/bin:$PATH"
export CUDACXX="${CUDACXX:-/usr/local/cuda/bin/nvcc}"
cd /home/unitree/llama.cpp
rm -rf build
try_cuda() {
  test -x "$CUDACXX" || return 1
  rm -rf build
  find /home/unitree/llama.cpp -path '*/build' -prune -o -type f -exec touch -m {} + 2>/dev/null || true
  cmake -S . -B build -G "Unix Makefiles" \\
    -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 -DCMAKE_CUDA_COMPILER="$CUDACXX" \\
    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DCMAKE_BUILD_TYPE=Release
  cmake --build build -j4 --target llama-server
}
try_cpu() {
  rm -rf build
  find /home/unitree/llama.cpp -path '*/build' -prune -o -type f -exec touch -m {} + 2>/dev/null || true
  cmake -S . -B build -G "Unix Makefiles" \\
    -DGGML_CUDA=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DCMAKE_BUILD_TYPE=Release
  cmake --build build -j4 --target llama-server
}
if [ "%USE_CUDA%" = "1" ] && try_cuda; then
  echo LLAMA_MODE=CUDA
elif try_cpu; then
  echo LLAMA_MODE=CPU
else
  exit 1
fi
test -x build/bin/llama-server
ls -la build/bin/llama-server
echo LLAMA_BUILD_OK
""".replace("%USE_CUDA%", "1" if use_cuda else "0")
    mode = "CUDA then CPU fallback" if use_cuda else "CPU (Jetson offline)"
    print(f"[llama pc->nx] build llama-server on NX ({mode}) …")
    _, stdout, stderr = ssh.exec_command(build_script, timeout=7200)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if out.strip():
        print(out[-3000:])
    if "LLAMA_BUILD_OK" not in out:
        raise RuntimeError(f"build failed: {err[-3000:]}")
    return "/home/unitree/llama.cpp/build/bin/llama-server"


def upload_gguf_to_nx(sftp: paramiko.SFTPClient, gguf: Path) -> str:
    remote_model_dir = f"{REMOTE_BASE}/models/gemma"
    remote_model = f"{remote_model_dir}/{MODEL_FILE}"
    print("[llama pc->nx] upload GGUF model …")
    _sftp_mkdirs(sftp, remote_model_dir)
    sftp.put(str(gguf), remote_model)
    print(f"  -> {remote_model} ({gguf.stat().st_size} B)")
    return remote_model


def main() -> None:
    ap = argparse.ArgumentParser(description="Install Gemma/llama-server on Jetson NX from PC")
    ap.add_argument(
        "--build-only",
        action="store_true",
        help="Salta upload sorgenti (llama.cpp gia sulla NX)",
    )
    ap.add_argument(
        "--binary-only",
        action="store_true",
        help="Solo sorgenti llama.cpp + build llama-server (senza GGUF)",
    )
    ap.add_argument(
        "--model-only",
        action="store_true",
        help="Solo GGUF + avvio llama-server (salta upload sorgenti e build)",
    )
    args = ap.parse_args()
    if args.model_only and args.binary_only:
        raise SystemExit("Usa --model-only oppure --binary-only, non entrambi")

    host = nx_host()
    print(f"[llama pc->nx] {nx_user()}@{host}")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=nx_user(), password=nx_password(), timeout=45)
    sftp = ssh.open_sftp()

    bin_path = "/home/unitree/llama.cpp/build/bin/llama-server"
    if args.model_only:
        print("[llama pc->nx] model-only: skip upload sorgenti e build")
        _, so, _ = ssh.exec_command(f"test -x {bin_path} && echo LLAMA_BIN_OK || echo LLAMA_BIN_MISSING", timeout=20)
        out = so.read().decode(errors="replace").strip()
        if "LLAMA_BIN_OK" not in out:
            raise SystemExit(
                f"Manca {bin_path} — prima: python scripts/install_gemma_from_pc_to_nx.py --binary-only --build-only"
            )
        print(f"[llama pc->nx] llama-server OK: {bin_path}")
    else:
        llama_src = _ensure_llama_sources()
        if not args.build_only:
            upload_llama_sources(ssh, sftp, llama_src)
        else:
            print("[llama pc->nx] build-only: skip upload sorgenti")

        _, so, _ = ssh.exec_command(f"test -x {bin_path} && echo LLAMA_BIN_OK", timeout=20)
        bin_ok = "LLAMA_BIN_OK" in so.read().decode(errors="replace")
        if bin_ok and args.build_only:
            print("[llama pc->nx] llama-server gia presente — skip rebuild")
        else:
            install_cmake_on_nx(ssh, sftp)
            bin_path = build_llama_server_on_nx(ssh)

    if not args.binary_only:
        gguf = _ensure_gguf()
        upload_gguf_to_nx(sftp, gguf)
        start_script = f"""set -e
mkdir -p {REMOTE_BASE}/logs
pkill -f 'nx_llama_supervise.sh' 2>/dev/null || true
pkill -f 'llama-server' 2>/dev/null || true
sleep 1
chmod +x {REMOTE_BASE}/scripts/nx_llama_supervise.sh
nohup bash {REMOTE_BASE}/scripts/nx_llama_supervise.sh >> {REMOTE_BASE}/logs/llama_supervise_boot.log 2>&1 &
sleep 4
curl -s -m 8 http://127.0.0.1:8080/v1/models | head -c 400 || echo LLAMA_HTTP_FAIL
"""
        print("[llama pc->nx] start llama-server …")
        _, stdout, stderr = ssh.exec_command(start_script, timeout=60)
        print(stdout.read().decode(errors="replace").strip())
        err = stderr.read().decode(errors="replace").strip()
        if err:
            print("start stderr:", err[-800:])
        verify = (
            f"cd {REMOTE_BASE} && GO2_HERMES_OPENAI_BASE_URL=http://127.0.0.1:8080/v1 "
            f"GO2_HERMES_MODEL={MODEL_ALIAS} python3 scripts/verify_gemma_local_http.py --prompt 'Ciao Hermes'"
        )
        print("[llama pc->nx] verify chat …")
        _, stdout, _ = ssh.exec_command(verify, timeout=120)
        print(stdout.read().decode(errors="replace").strip())
        print("[llama pc->nx] OK - Hermes :5052 + Gemma su http://127.0.0.1:8080/v1")
    else:
        print(f"[llama pc->nx] OK binary {bin_path}")
        print("[llama pc->nx] Manca GGUF: set HF_TOKEN o GO2_GEMMA_GGUF_LOCAL e rilancia senza --binary-only")

    sftp.close()
    ssh.close()


if __name__ == "__main__":
    main()

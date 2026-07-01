#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/bin"
gcc -shared -fPIC -O2 "$ROOT/scripts/free_iox_chunk_shim.c" -o "$ROOT/bin/libd1_iox_shim.so"
echo "OK: $ROOT/bin/libd1_iox_shim.so"

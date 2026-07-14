#!/usr/bin/env python3
"""Verify unitree_sdk2py + CRC libs before arm motion (read-only)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from h2_common import SDK_DIR, ensure_sdk_path, init_dds, wait_lowstate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", default=None)
    args = parser.parse_args()

    crc = SDK_DIR / "unitree_sdk2py" / "utils" / "lib" / "crc_aarch64.so"
    if not crc.is_file():
        print(f"MISSING {crc} — scaricare da unitree_sdk2_python ufficiale", file=sys.stderr)
        return 1
    print(f"[verify] crc lib OK ({crc.stat().st_size} bytes)")

    ensure_sdk_path()
    import unitree_sdk2py  # noqa: F401
    from unitree_sdk2py.utils.crc import CRC

    CRC()
    print("[verify] CRC() OK")

    iface = init_dds(args.iface)
    msg = wait_lowstate()
    print(f"[verify] lowstate OK motors={len(msg.motor_state)} iface={iface}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

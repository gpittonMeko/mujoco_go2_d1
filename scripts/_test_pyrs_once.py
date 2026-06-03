#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from go2_dashboard import realsense_pyrs as r

r.stop()
ok = r.start()
print("start", ok, r.status())
b = r.read_bundle()
if b and b.color is not None:
    print("frame", b.color.shape)
else:
    print("no frame", r.status())

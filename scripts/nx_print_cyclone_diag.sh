#!/bin/bash
# Diagnostica rapida CycloneDDS + unitree_sdk2py sulla Jetson (nessun comando al cane).
set +e
echo "=== nx_print_cyclone_diag $(date -Is) ==="
echo "=== which python3 ==="
which python3 2>/dev/null || true
python3 -V 2>/dev/null || true
echo ""
echo "=== pip cyclonedds / unitree (se installati) ==="
python3 -m pip show cyclonedds 2>/dev/null | head -n 12 || echo "(pip show cyclonedds: n/d)"
python3 -m pip list 2>/dev/null | grep -iE 'unitree|cyclone' | head -n 8 || true
echo ""
echo "=== ldd libddsc usata da cyclonedds._clayer ==="
python3 -c "import cyclonedds._clayer as _c; print(_c.__file__)" 2>/dev/null | while read -r so; do
  [ -n "$so" ] && ldd "$so" 2>/dev/null | grep -F libddsc || true
done
echo ""
echo "=== sys.path (prime 8 voci) ==="
python3 -c "import sys; print('\n'.join(sys.path[:8]))" 2>&1
echo ""
echo "=== unitree_sdk2py (primo match) ==="
python3 -c "import unitree_sdk2py,inspect; import os; p=os.path.dirname(inspect.getfile(unitree_sdk2py)); print(p)" 2>&1 || echo "(import unitree_sdk2py fallito)"
echo ""
echo "=== GO2_DDS / UNITREE env ==="
env 2>/dev/null | grep -E '^(GO2_DDS|UNITREE_SDK2|CYCLONE|CMAKE_PREFIX|ROS_DOMAIN|RMW_)' | sort || true
echo ""
echo "=== riferimenti GitHub (segfault Jetson / Cyclone) ==="
echo "https://github.com/unitreerobotics/unitree_sdk2_python/issues/88"
echo "https://github.com/unitreerobotics/unitree_sdk2_python/issues/53"
echo "=== fine ==="

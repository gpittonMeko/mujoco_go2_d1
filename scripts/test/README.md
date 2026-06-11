# Test script (unit / sim / CI locale)

| Script | Quando usarlo |
|--------|----------------|
| `test_dashboard_smoke.py` | Gate principale: Flask in-process, route e template (**solo PC**, non sulla NX) |
| `test_grasp_assessment.py` | Unit test logica grasp assessment |
| `test_grasp_overlay.py` | Unit test overlay 2D |
| `test_camera_usb_mapping.py` | Unit test mapping V4L |
| `test_dds_lowstate.py` | Sim MuJoCo: ricezione LowState |
| `test_movimento.py` | Sim/real: stand/squat via LowCmd |
| `test_arm_move.py` | Sim minimale J1 (preferire `test_movimento.py` per esperimenti più completi) |

Smoke HTTP verso NX/PC: usare **`scripts/verify_go2_lab.py`** (non duplicare qui).

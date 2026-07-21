#!/usr/bin/env python3
"""Test di accettazione hand-eye: i sample sono geometricamente COERENTI?

Idea chiave (indipendente dalla trasformazione mano-occhio X): tool e camera
sono lo STESSO corpo rigido, quindi l'angolo di rotazione RELATIVA tra due pose
misurato dalla FK (dai servo) DEVE coincidere con quello misurato dalla camera
(dal marker). Se differiscono di molto, i sample sono inutilizzabili per la
hand-eye a prescindere dal solver (era la causa dei residui 20cm/50deg).

La FK usata e' identica all'URDF reale del D1 (d1_550_description), quindi se la
FK e la camera non concordano il problema e' nei dati camera (posa marker), non
nella cinematica.

Uso:
  python scripts/check_handeye_consistency.py            # scarica i sample dalla NX
  python scripts/check_handeye_consistency.py file.json  # usa un file locale
"""
from __future__ import annotations

import json
import math
import sys

import numpy as np

try:
    import cv2  # per Rodrigues; se manca uso fallback
    _HAS_CV2 = True
except Exception:  # noqa: BLE001
    _HAS_CV2 = False

NX_HOST = "192.168.123.18"
NX_USER = "unitree"
NX_PASS = "123"
NX_SAMPLES = "/home/unitree/go2_visual_dashboard/data/d1_grasp6d_handeye_samples.json"

# Catena cinematica D1 = URDF reale d1_550_description (origine, asse) per giunto.
D1_CHAIN = [
    (np.array([0.0, 0.0, 0.0738]), np.array([0.0, 0.0, -1.0])),
    (np.array([0.0, -0.0276, 0.0578]), np.array([0.0, 1.0, 0.0])),
    (np.array([0.0, -0.0004, 0.27]), np.array([0.0, 1.0, 0.0])),
    (np.array([0.05, 0.0275, 0.041325]), np.array([1.0, 0.0, 0.0])),
    (np.array([0.15468, -0.0258, 0.0001]), np.array([0.0, 1.0, 0.0])),
    (np.array([0.0777, 0.025822, -0.0010718]), np.array([1.0, 0.0, 0.0])),
]

# Verdetto: sotto GOOD_DEG il set e' coerente; sopra BAD_DEG e' rotto.
GOOD_DEG = 3.0
BAD_DEG = 8.0


def _axis_angle(axis: np.ndarray, ang: float) -> np.ndarray:
    axis = axis / np.linalg.norm(axis)
    x, y, z = axis
    c, s = math.cos(ang), math.sin(ang)
    C = 1 - c
    return np.array([
        [c + x * x * C, x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, c + z * z * C],
    ])


def fk_R(servo_deg: list[float]) -> np.ndarray:
    q = np.radians(np.asarray(servo_deg[:6], dtype=float))
    R = np.eye(3)
    for i, (_bp, axis) in enumerate(D1_CHAIN):
        R = R @ _axis_angle(axis, q[i])
    return R


def rot_angle_deg(R: np.ndarray) -> float:
    return math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(R) - 1.0) * 0.5))))


def load_samples(arg: str | None) -> list[dict]:
    if arg and arg not in ("--nx", "-nx"):
        return json.load(open(arg, encoding="utf-8"))
    import paramiko

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(NX_HOST, username=NX_USER, password=NX_PASS, timeout=25)
    sftp = ssh.open_sftp()
    with sftp.open(NX_SAMPLES) as f:
        data = json.loads(f.read().decode())
    sftp.close()
    ssh.close()
    if isinstance(data, dict):
        data = data.get("samples") or data.get("data") or []
    return data


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    samples = load_samples(arg)
    n = len(samples)
    print(f"[check] {n} sample da {'NX' if not arg or arg.startswith('--') else arg}")
    if n < 2:
        print("[check] servono almeno 2 sample.")
        return 1

    Rt, Rc, ok_idx = [], [], []
    for k, s in enumerate(samples):
        servo = s.get("servo_deg")
        Tct = s.get("T_camera_target")
        if not servo or not Tct:
            print(f"  #{k}: manca servo_deg o T_camera_target -> skip")
            continue
        Rt.append(fk_R(servo))
        Rc.append(np.asarray(Tct, dtype=float)[:3, :3])
        ok_idx.append(k)

    diffs = []
    worst = (0.0, None, None)
    print("\n  coppia | angolo_tool | angolo_camera | |diff|")
    for a in range(len(ok_idx)):
        for b in range(a + 1, len(ok_idx)):
            at = rot_angle_deg(Rt[a].T @ Rt[b])
            ac = rot_angle_deg(Rc[a].T @ Rc[b])
            d = abs(at - ac)
            diffs.append(d)
            if d > worst[0]:
                worst = (d, ok_idx[a], ok_idx[b])
            print(f"  {ok_idx[a]:>2}-{ok_idx[b]:<2}  | {at:8.1f}deg | {ac:8.1f}deg | {d:6.1f}deg")

    if not diffs:
        print("[check] nessuna coppia valida.")
        return 1
    diffs_np = np.array(diffs)
    med = float(np.median(diffs_np))
    mean = float(np.mean(diffs_np))
    mx = float(np.max(diffs_np))
    print(f"\n[check] |diff| angoli rotazione relativa tool-vs-camera:")
    print(f"        mediana={med:.1f}deg  media={mean:.1f}deg  max={mx:.1f}deg (coppia {worst[1]}-{worst[2]})")

    if med <= GOOD_DEG:
        print(f"[VERDETTO] OK: set COERENTE (mediana <= {GOOD_DEG}deg). La hand-eye puo' convergere.")
        return 0
    if med >= BAD_DEG:
        print(f"[VERDETTO] ROTTO: set INCOERENTE (mediana >= {BAD_DEG}deg).")
        print("           Le pose camera non corrispondono al moto del braccio: probabile")
        print("           ambiguita' del marker o pose troppo estreme/frontali. NON calibrare,")
        print("           scarta i sample peggiori (vedi coppie con |diff| alto) e ricattura")
        print("           con griglia piu' INCLINATA e rotazioni polso attorno ad assi diversi.")
        return 2
    print(f"[VERDETTO] DUBBIO ({GOOD_DEG}<mediana<{BAD_DEG}deg): togli i sample delle coppie peggiori.")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())

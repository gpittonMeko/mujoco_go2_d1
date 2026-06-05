"""Preview JPEG per ogni uscita RealSense (RGB, depth, IR1, IR2, YOLO, griglia)."""

from __future__ import annotations

import os
from typing import Any

import numpy as np


def _jpeg_quality() -> int:
    return max(55, min(90, int(os.environ.get("VISION_STREAM_JPEG_QUALITY", "72"))))


def depth_colormap_bgr(depth_mm: np.ndarray | None, cv2: Any) -> np.ndarray | None:
    if depth_mm is None or depth_mm.size == 0:
        return None
    d = depth_mm.astype(np.float32)
    valid = d > 0
    if not np.any(valid):
        return None
    vmin = float(np.percentile(d[valid], 5))
    vmax = float(np.percentile(d[valid], 95))
    span = max(vmax - vmin, 80.0)
    norm = np.clip((d - vmin) / span, 0.0, 1.0)
    u8 = (norm * 255.0).astype(np.uint8)
    u8[~valid] = 0
    return cv2.applyColorMap(u8, cv2.COLORMAP_TURBO)


def ir_to_bgr(ir: np.ndarray | None, cv2: Any) -> np.ndarray | None:
    if ir is None or ir.size == 0:
        return None
    if ir.ndim == 3:
        return ir.copy()
    return cv2.cvtColor(ir, cv2.COLOR_GRAY2BGR)


def encode_jpeg(frame: np.ndarray | None, cv2: Any) -> bytes | None:
    if frame is None or frame.size == 0:
        return None
    q = _jpeg_quality()
    ok, enc = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), q])
    return enc.tobytes() if ok else None


def placeholder_bgr(
    cv2: Any,
    *,
    w: int = 640,
    h: int = 480,
    title: str = "N/D",
    subtitle: str = "",
) -> np.ndarray:
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (28, 32, 40)
    cv2.putText(img, title, (16, h // 2 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (140, 160, 200), 2)
    if subtitle:
        cv2.putText(img, subtitle[:56], (16, h // 2 + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (100, 120, 150), 1)
    return img


def bundle_preview_jpegs(bundle: dict[str, Any] | None, cv2: Any) -> dict[str, bytes | None]:
    """JPEG per ogni pannello UI."""
    keys = ("color", "depth", "ir1", "ir2", "grid")
    out: dict[str, bytes | None] = {k: None for k in keys}
    h, w = 480, 640
    if bundle and bundle.get("color") is not None:
        h, w = bundle["color"].shape[:2]

    if not bundle:
        ph = placeholder_bgr(cv2, w=w, h=h, title="RealSense", subtitle="in attesa frame…")
        out["color"] = encode_jpeg(ph, cv2)
        for k in ("depth", "ir1", "ir2"):
            out[k] = encode_jpeg(
                placeholder_bgr(cv2, w=w, h=h, title=k.upper(), subtitle="no bundle"),
                cv2,
            )
        return out

    color = bundle.get("color")
    out["color"] = encode_jpeg(color, cv2)

    d_vis = depth_colormap_bgr(bundle.get("depth_mm"), cv2)
    if d_vis is None:
        d_vis = placeholder_bgr(cv2, w=w, h=h, title="DEPTH", subtitle="stream non attivo")
    out["depth"] = encode_jpeg(d_vis, cv2)

    ir1 = ir_to_bgr(bundle.get("ir1") or bundle.get("ir"), cv2)
    if ir1 is None:
        ir1 = placeholder_bgr(cv2, w=w, h=h, title="IR1", subtitle="sinistro / stream 1")
    out["ir1"] = encode_jpeg(ir1, cv2)

    ir2 = ir_to_bgr(bundle.get("ir2"), cv2)
    if ir2 is None:
        ir2 = placeholder_bgr(cv2, w=w, h=h, title="IR2", subtitle="destro / stream 2")
    out["ir2"] = encode_jpeg(ir2, cv2)

    cells = [
        ("RGB", color),
        ("Depth", d_vis),
        ("IR1", ir1),
        ("IR2", ir2),
    ]
    thumb_h = int(os.environ.get("VISION_GRID_CELL_H", str(max(120, h // 3))))
    thumb_w = int(thumb_h * w / max(h, 1))
    thumbs = []
    for lab, fr in cells:
        if fr is None:
            fr = placeholder_bgr(cv2, w=w, h=h, title=lab)
        cell = cv2.resize(fr, (thumb_w, thumb_h), interpolation=cv2.INTER_AREA)
        cv2.rectangle(cell, (0, 0), (thumb_w - 1, 20), (0, 0, 0), -1)
        cv2.putText(cell, lab, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (230, 240, 255), 1)
        thumbs.append(cell)
    out["grid"] = encode_jpeg(np.vstack([np.hstack(thumbs[:2]), np.hstack(thumbs[2:])]), cv2)
    return out

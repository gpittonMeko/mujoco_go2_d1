#!/usr/bin/env python3
"""Test stream colore RealSense via pyrealsense2 (ROS noetic)."""
try:
    import pyrealsense2 as rs
    import numpy as np

    print("pyrealsense2:", rs.__file__)
    ctx = rs.context()
    devs = ctx.query_devices()
    print("devices:", len(devs))
    for d in devs:
        name_attr = getattr(rs.camera_info, "name", None) or getattr(rs.camera_info, "NAME", None)
        print(" ", d.get_info(name_attr) if name_attr is not None else d)
    if len(devs) < 1:
        raise SystemExit("no device")

    cfg = rs.config()
    cfg.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 15)
    pipe = rs.pipeline()
    pipe.start(cfg)
    for i in range(12):
        frames = pipe.wait_for_frames(8000)
        c = frames.get_color_frame()
        if not c:
            print("no color frame", i)
            continue
        img = np.asanyarray(c.get_data())
        chroma = float(
            np.abs(img[:, :, 0].astype(int) - img[:, :, 1].astype(int)).mean()
            + np.abs(img[:, :, 1].astype(int) - img[:, :, 2].astype(int)).mean()
        )
        print(f"frame {i} shape={img.shape} max={img.max()} chroma={chroma:.2f}")
    pipe.stop()
    print("RGB_STREAM_OK")
except Exception as exc:
    print("FAIL", type(exc).__name__, exc)
    raise

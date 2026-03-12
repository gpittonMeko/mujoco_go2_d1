import time
import signal
import socket
import json
import mujoco
import mujoco.viewer
from threading import Thread
import threading
import numpy as np

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py_bridge import UnitreeSdk2Bridge, ElasticBand

import config

if config.ENABLE_DEPTH_CAMERA:
    import cv2 as cv
    from image_publisher.image_publisher import DepthImagePublisher

BALL_UDP_PORT = 9870
GRAB_ATTRACT_DIST = 0.15
GRAB_WELD_DIST = 0.08
GRAB_FORCE = 1200.0

locker = threading.Lock()

mj_model = mujoco.MjModel.from_xml_path(config.ROBOT_SCENE)
mj_data = mujoco.MjData(mj_model)

try:
    khome = mujoco.mj_name2id(mj_model, mujoco.mjtObj.mjOBJ_KEY, "home")
    if khome >= 0:
        mujoco.mj_resetDataKeyframe(mj_model, mj_data, khome)
except Exception:
    pass

BALL_INIT_POS = [1.0, 0.0, 0.355]
_ball_body_id = -1
_ball_qadr = -1
_ball_qvel_adr = -1
_tool_body_id = -1
try:
    _ball_body_id = mj_model.body("red_ball").id
    _tool_body_id = mj_model.body("arm_link06").id
    jadr = mj_model.body_jntadr[_ball_body_id]
    if jadr >= 0:
        _ball_qadr = mj_model.jnt_qposadr[jadr]
        _ball_qvel_adr = mj_model.jnt_dofadr[jadr]
        mj_data.qpos[_ball_qadr:_ball_qadr+3] = BALL_INIT_POS
        mj_data.qpos[_ball_qadr+3:_ball_qadr+7] = [1, 0, 0, 0]
except Exception:
    pass

ball_grabbed = False

if config.ENABLE_ELASTIC_BAND:
    elastic_band = ElasticBand()
    if config.ROBOT == "h1" or config.ROBOT == "g1":
        band_attached_link = mj_model.body("torso_link").id
    else:
        band_attached_link = mj_model.body("base_link").id
    viewer = mujoco.viewer.launch_passive(
        mj_model, mj_data, key_callback=elastic_band.MujuocoKeyCallback
    )
else:
    viewer = mujoco.viewer.launch_passive(mj_model, mj_data)

if config.ENABLE_DEPTH_CAMERA:
    depth_image = np.zeros(
        (config.CAMERA_DOWNSAMPLED_HEIGHT, config.CAMERA_DOWNSAMPLED_WIDTH),
        dtype=np.float32
    )

mj_model.opt.timestep = config.SIMULATE_DT
num_motor_ = mj_model.nu
dim_motor_sensor_ = 3 * num_motor_

time.sleep(0.2)


def apply_magnetic_grab():
    """Apply magnetic force between arm_link06 tip and red_ball. Returns grab distance."""
    global ball_grabbed
    if _ball_body_id < 0 or _tool_body_id < 0:
        return 999.0

    tool_pos = mj_data.xpos[_tool_body_id].copy()
    tool_fwd = mj_data.xmat[_tool_body_id].reshape(3, 3)[:, 0]
    tip_pos = tool_pos + 0.07 * tool_fwd

    ball_pos = mj_data.xpos[_ball_body_id].copy()
    diff = tip_pos - ball_pos
    dist = float(np.linalg.norm(diff))

    if dist < GRAB_WELD_DIST:
        ball_grabbed = True
        if _ball_qadr >= 0:
            mj_data.qpos[_ball_qadr:_ball_qadr+3] = tip_pos
            if _ball_qvel_adr >= 0:
                mj_data.qvel[_ball_qvel_adr:_ball_qvel_adr+6] = 0
        mj_data.xfrc_applied[_ball_body_id, :3] = 0
    elif dist < GRAB_ATTRACT_DIST and dist > 0.001:
        strength = GRAB_FORCE * ((1.0 - dist / GRAB_ATTRACT_DIST) ** 2)
        force = strength * diff / dist
        mj_data.xfrc_applied[_ball_body_id, :3] = force
    else:
        if not ball_grabbed:
            mj_data.xfrc_applied[_ball_body_id, :3] = 0

    if ball_grabbed and _ball_qadr >= 0:
        mj_data.qpos[_ball_qadr:_ball_qadr+3] = tip_pos
        if _ball_qvel_adr >= 0:
            mj_data.qvel[_ball_qvel_adr:_ball_qvel_adr+6] = 0

    return dist


def SimulationThread():
    global mj_data, mj_model

    ChannelFactoryInitialize(config.DOMAIN_ID, config.INTERFACE)
    unitree = UnitreeSdk2Bridge(mj_model, mj_data)

    if config.ENABLE_DEPTH_CAMERA:
        depth_pub = DepthImagePublisher(depth_image, config.DEPTH_PUBLISH_DT)

    if config.USE_JOYSTICK:
        unitree.SetupJoystick(device_id=0, js_type=config.JOYSTICK_TYPE)
    if config.PRINT_SCENE_INFORMATION:
        unitree.PrintSceneInformation()

    while viewer.is_running():
        step_start = time.perf_counter()
        locker.acquire()

        if config.ENABLE_ELASTIC_BAND:
            if elastic_band.enable:
                mj_data.xfrc_applied[band_attached_link, :3] = elastic_band.Advance(
                    mj_data.qpos[:3], mj_data.qvel[:3]
                )
        mujoco.mj_step(mj_model, mj_data)
        apply_magnetic_grab()

        locker.release()

        time_until_next_step = mj_model.opt.timestep - (
            time.perf_counter() - step_start
        )
        if time_until_next_step > 0:
            time.sleep(time_until_next_step)


def quat_to_rot(q):
    w, x, y, z = q
    return np.array([
        [1-2*(y*y+z*z), 2*(x*y-w*z), 2*(x*z+w*y)],
        [2*(x*y+w*z), 1-2*(x*x+z*z), 2*(y*z-w*x)],
        [2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y)]
    ])


def ball_in_robot_frame():
    try:
        ball_id = mj_model.body("red_ball").id
        base_id = mj_model.body("base_link").id
    except Exception:
        return None
    R = quat_to_rot(mj_data.xquat[base_id])
    diff = mj_data.xpos[ball_id] - mj_data.xpos[base_id]
    return (R.T @ diff).tolist()


def detect_red_ball(rgb, depth_raw):
    hsv = cv.cvtColor(rgb, cv.COLOR_RGB2HSV)
    mask1 = cv.inRange(hsv, np.array([0, 100, 80]), np.array([10, 255, 255]))
    mask2 = cv.inRange(hsv, np.array([170, 100, 80]), np.array([180, 255, 255]))
    mask = cv.morphologyEx(mask1 | mask2, cv.MORPH_OPEN, np.ones((5, 5), np.uint8))
    mask = cv.morphologyEx(mask, cv.MORPH_DILATE, np.ones((5, 5), np.uint8))
    contours, _ = cv.findContours(mask, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    c = max(contours, key=cv.contourArea)
    if cv.contourArea(c) < 30:
        return None
    (cx, cy), radius = cv.minEnclosingCircle(c)
    cx, cy, radius = int(cx), int(cy), int(radius)
    patch = depth_raw[max(0,cy-3):min(depth_raw.shape[0],cy+4),
                      max(0,cx-3):min(depth_raw.shape[1],cx+4)]
    valid = patch[(patch > config.NEAR_CLIP) & (patch < config.FAR_CLIP)]
    depth_m = float(np.median(valid)) if len(valid) > 0 else -1.0
    return (cx, cy, depth_m, radius)


def pixel_to_cam_3d(cx, cy, depth_m, img_w, img_h, fovy_deg):
    fy = img_h / (2.0 * np.tan(np.deg2rad(fovy_deg) / 2.0))
    return ((cx - img_w/2.0)/fy * depth_m,
            (cy - img_h/2.0)/fy * depth_m,
            depth_m)


WRIST_CAM_NAME = "wrist_camera"
WRIST_W, WRIST_H = 320, 240


def render_camera(renderer, data, cam_name):
    try:
        renderer.update_scene(data, camera=cam_name)
        rgb = renderer.render().copy()
        renderer.enable_depth_rendering()
        depth = renderer.render().copy()
        renderer.disable_depth_rendering()
        return rgb, depth
    except Exception:
        return None, None


def annotate(bgr, det, fovy, w, h, prefix=""):
    if det is not None:
        cx, cy, dm, r = det
        cv.circle(bgr, (cx, cy), r, (0, 255, 0), 2)
        x3, y3, z3 = pixel_to_cam_3d(cx, cy, dm, w, h, fovy)
        cv.putText(bgr, f"d={dm:.2f}m", (cx-40, cy-r-8),
                   cv.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)
        cv.putText(bgr, f"{prefix}DETECTED", (8, 20),
                   cv.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
        return True
    cv.putText(bgr, f"{prefix}SEARCHING", (8, 20),
               cv.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
    return False


def PhysicsViewerThread():
    global depth_image

    body_ren = wrist_ren = None
    has_wrist = False
    if config.ENABLE_DEPTH_CAMERA:
        body_ren = mujoco.Renderer(mj_model, config.CAMERA_ORIGINAL_HEIGHT, config.CAMERA_ORIGINAL_WIDTH)
        try:
            wid = mujoco.mj_name2id(mj_model, mujoco.mjtObj.mjOBJ_CAMERA, WRIST_CAM_NAME)
            if wid >= 0:
                wrist_ren = mujoco.Renderer(mj_model, WRIST_H, WRIST_W)
                has_wrist = True
                print(f"Wrist camera attiva (ID={wid})")
        except Exception:
            pass

    body_fovy = wrist_fovy = 62.0
    try:
        cid = mujoco.mj_name2id(mj_model, mujoco.mjtObj.mjOBJ_CAMERA, config.CAMERA_SENSOR_NAME)
        if cid >= 0: body_fovy = float(mj_model.cam_fovy[cid])
        if has_wrist:
            wid = mujoco.mj_name2id(mj_model, mujoco.mjtObj.mjOBJ_CAMERA, WRIST_CAM_NAME)
            wrist_fovy = float(mj_model.cam_fovy[wid])
    except Exception:
        pass

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    while viewer.is_running():
        t0 = time.perf_counter()
        locker.acquire()
        viewer.sync()

        bl = ball_in_robot_frame()
        rz = float(mj_data.xpos[mj_model.body("base_link").id][2]) if bl else 0.33
        grab_dist = 999.0
        if _tool_body_id >= 0 and _ball_body_id >= 0:
            grab_dist = float(np.linalg.norm(
                mj_data.xpos[_tool_body_id] - mj_data.xpos[_ball_body_id]))

        bd = wd = False
        w_depth = -1.0
        w_pix = [0.0, 0.0]
        w_center_delta = [0.0, 0.0]

        if body_ren:
            try:
                rgb, dep = render_camera(body_ren, mj_data, config.CAMERA_SENSOR_NAME)
                if rgb is not None:
                    rd = cv.resize(dep, (config.CAMERA_DOWNSAMPLED_WIDTH, config.CAMERA_DOWNSAMPLED_HEIGHT))
                    rd = np.clip(rd, config.NEAR_CLIP, config.FAR_CLIP)
                    rd = (rd - config.NEAR_CLIP)/(config.FAR_CLIP - config.NEAR_CLIP)
                    depth_image[:] = rd
                    cv.imshow("Depth", cv.applyColorMap((255*rd).astype(np.uint8), cv.COLORMAP_JET))
                    bgr = cv.cvtColor(rgb, cv.COLOR_RGB2BGR)
                    det = detect_red_ball(rgb, dep)
                    bd = annotate(bgr, det, body_fovy,
                                  config.CAMERA_ORIGINAL_WIDTH, config.CAMERA_ORIGINAL_HEIGHT, "BODY ")
                    if ball_grabbed:
                        cv.putText(bgr, "PALLA PRESA!", (8, 50),
                                   cv.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)
                    dw = 320
                    dh = int(config.CAMERA_ORIGINAL_HEIGHT * dw / config.CAMERA_ORIGINAL_WIDTH)
                    cv.imshow("Body Camera", cv.resize(bgr, (dw, dh)))
            except Exception as e:
                print("Body cam err:", e)

        if wrist_ren:
            try:
                rgb, dep = render_camera(wrist_ren, mj_data, WRIST_CAM_NAME)
                if rgb is not None:
                    wbgr = cv.cvtColor(rgb, cv.COLOR_RGB2BGR)
                    wdet = detect_red_ball(rgb, dep)
                    wd = annotate(wbgr, wdet, wrist_fovy, WRIST_W, WRIST_H, "WRIST ")
                    if wdet:
                        cx, cy, dm, _ = wdet
                        w_depth = dm
                        px = (cx - WRIST_W/2) / (WRIST_W/2)
                        py = (cy - WRIST_H/2) / (WRIST_H/2)
                        w_pix = [px, py]
                        w_center_delta = [0.025 * px, 0.025 * py]
                    else:
                        w_pix = [0.0, 0.0]
                        w_center_delta = [0.0, 0.0]
                    if ball_grabbed:
                        cv.putText(wbgr, "PALLA PRESA!", (8, 50),
                                   cv.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2)
                    cv.imshow("Wrist Camera", wbgr)
            except Exception as e:
                print("Wrist cam err:", e)

        cv.waitKey(1)

        msg = json.dumps({
            "detected": bd, "pos": bl or [0,0,0], "robot_z": rz,
            "wrist_detected": wd, "wrist_depth": w_depth, "wrist_pixel": w_pix,
            "wrist_center_delta": w_center_delta,
            "grabbed": ball_grabbed, "grab_dist": grab_dist,
        })
        try: udp.sendto(msg.encode(), ("127.0.0.1", BALL_UDP_PORT))
        except Exception: pass

        locker.release()
        dt = config.VIEWER_DT - (time.perf_counter() - t0)
        if dt > 0: time.sleep(dt)


def _cleanup():
    try: viewer.close()
    except Exception: pass
    if config.ENABLE_DEPTH_CAMERA:
        try: cv.destroyAllWindows()
        except Exception: pass


if __name__ == "__main__":
    vt = Thread(target=PhysicsViewerThread)
    st = Thread(target=SimulationThread)
    signal.signal(signal.SIGINT, lambda s, f: (_cleanup(),))
    vt.start(); st.start()
    try:
        vt.join(); st.join()
    except KeyboardInterrupt:
        _cleanup(); vt.join(timeout=2); st.join(timeout=2)

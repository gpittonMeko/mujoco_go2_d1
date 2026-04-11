import time
import math
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
WRIST_CAM_CTL_PORT = 9871  # run_go2_d1_ball.py → inclinazione wrist cam in fase presa (solo runtime)
GRAB_ATTRACT_DIST = 0.22
GRAB_WELD_DIST = 0.11
GRAB_FORCE = 2520.0  # +40% attrazione verso il tool tip
# Estensione lungo asse X di arm_link06 (allineata a arm_kinematics fk_tool_tip +0.07)
GRAB_TIP_EXTEND = 0.07

locker = threading.Lock()

mj_model = mujoco.MjModel.from_xml_path(config.ROBOT_SCENE)
mj_data = mujoco.MjData(mj_model)

try:
    khome = mujoco.mj_name2id(mj_model, mujoco.mjtObj.mjOBJ_KEY, "home")
    if khome >= 0:
        mujoco.mj_resetDataKeyframe(mj_model, mj_data, khome)
        # Il keyframe "home" ha solo 25 valori (robot). I corpi free (red_ball, ecc.)
        # vengono azzerati. Ripristiniamo le posizioni dal modello (XML).
        n_robot = 25  # 7 free + 12 gambe + 6 braccio
        if mj_model.nq > n_robot and hasattr(mj_model, "qpos0"):
            mj_data.qpos[n_robot:] = mj_model.qpos0[n_robot:]
except Exception:
    pass

BALL_INIT_POS = [1.0, 0.2, 0.045]  # come scene*.xml: x=1.0 iniziale, +0.2 m su Y (sinistra robot)
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

# Wrist camera: quat base da MJCF (ogni avvio sim = come nel progetto); in presa si applica +45° locale.
_wrist_cam_mj_id = -1
_wrist_cam_quat_base = _wrist_cam_quat_grasp = None
_wrist_grasp_cam = False
_wrist_ctl_sock = None
try:
    _wrist_cam_mj_id = mujoco.mj_name2id(
        mj_model, mujoco.mjtObj.mjOBJ_CAMERA, "wrist_camera")
    if _wrist_cam_mj_id >= 0:
        _wrist_cam_quat_base = np.array(
            mj_model.cam_quat[_wrist_cam_mj_id], dtype=np.float64).copy()
        _dq = np.zeros(4, dtype=np.float64)
        mujoco.mju_axisAngle2Quat(
            _dq, np.array([1.0, 0.0, 0.0], dtype=np.float64), math.pi / 4.0)
        _wrist_cam_quat_grasp = np.zeros(4, dtype=np.float64)
        mujoco.mju_mulQuat(
            _wrist_cam_quat_grasp, _wrist_cam_quat_base, _dq)
        _wrist_ctl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _wrist_ctl_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _wrist_ctl_sock.bind(("127.0.0.1", WRIST_CAM_CTL_PORT))
        _wrist_ctl_sock.setblocking(False)
except Exception as _e:
    print("wrist_camera ctl:", _e)

ball_grabbed = False


def _tool_tip_world():
    """Punta utensile in world frame (stessa convenzione della presa magnetica)."""
    if _tool_body_id < 0:
        return None
    tool_pos = mj_data.xpos[_tool_body_id].copy()
    tool_fwd = mj_data.xmat[_tool_body_id].reshape(3, 3)[:, 0]
    return tool_pos + GRAB_TIP_EXTEND * tool_fwd


def _poll_wrist_cam_control():
    """Legge comandi UDP da run_go2_d1_ball (solo memoria; MJCF su disco resta invariato)."""
    global _wrist_grasp_cam
    if _wrist_ctl_sock is None:
        return
    try:
        while True:
            data, _ = _wrist_ctl_sock.recvfrom(512)
            m = json.loads(data.decode())
            _wrist_grasp_cam = bool(m.get("wrist_grasp_cam", False))
    except BlockingIOError:
        pass
    except Exception:
        pass


def _apply_wrist_cam_orientation():
    if _wrist_cam_mj_id < 0 or _wrist_cam_quat_base is None:
        return
    if _wrist_grasp_cam and _wrist_cam_quat_grasp is not None:
        mj_model.cam_quat[_wrist_cam_mj_id][:] = _wrist_cam_quat_grasp
    else:
        mj_model.cam_quat[_wrist_cam_mj_id][:] = _wrist_cam_quat_base

# Arm Motors manual override (slider values -> ctrl, solo braccio)
arm_manual_ctrl = None
arm_trackbars_created = False

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

# Viewer camera: keep fixed on robot with larger distance.
try:
    base_id = mj_model.body("base_link").id
    viewer.cam.trackbodyid = base_id
    viewer.cam.distance = 2.2
    viewer.cam.azimuth = 140
    viewer.cam.elevation = -20
except Exception:
    pass

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

    tip_pos = _tool_tip_world()
    if tip_pos is None:
        return 999.0

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
        if arm_manual_ctrl is not None and mj_model.nu >= 18:
            for i in range(6):
                mj_data.ctrl[12 + i] = arm_manual_ctrl[i]
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
    s_min = int(getattr(config, "BALL_HSV_S_MIN", 100))
    v_min = int(getattr(config, "BALL_HSV_V_MIN", 80))
    h1m = int(getattr(config, "BALL_HSV_H1_MAX", 10))
    h2m = int(getattr(config, "BALL_HSV_H2_MIN", 170))
    lo = np.array([0, s_min, v_min], dtype=np.uint8)
    mask1 = cv.inRange(hsv, lo, np.array([h1m, 255, 255], dtype=np.uint8))
    mask2 = cv.inRange(
        hsv, np.array([h2m, s_min, v_min], dtype=np.uint8),
        np.array([180, 255, 255], dtype=np.uint8),
    )
    ko = int(getattr(config, "BALL_MORPH_OPEN_K", 5))
    kd = int(getattr(config, "BALL_MORPH_DILATE_K", 5))
    ko = ko + (1 - ko % 2)
    kd = kd + (1 - kd % 2)
    k_open = np.ones((ko, ko), np.uint8)
    k_dil = np.ones((kd, kd), np.uint8)
    mask = cv.morphologyEx(mask1 | mask2, cv.MORPH_OPEN, k_open)
    mask = cv.morphologyEx(mask, cv.MORPH_DILATE, k_dil)
    contours, _ = cv.findContours(mask, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    c = max(contours, key=cv.contourArea)
    min_area = float(getattr(config, "BALL_MIN_CONTOUR_AREA", 30))
    if cv.contourArea(c) < min_area:
        return None
    (cx, cy), radius = cv.minEnclosingCircle(c)
    cx, cy, radius = int(cx), int(cy), int(radius)
    pr = int(getattr(config, "BALL_DEPTH_PATCH_RADIUS", 3))
    pr = max(2, pr)
    patch = depth_raw[max(0, cy - pr): min(depth_raw.shape[0], cy + pr + 1),
                      max(0, cx - pr): min(depth_raw.shape[1], cx + pr + 1)]
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


# Geometria braccio Z1 per vista 2D
_L_UPPER = 0.35
_L_FORE_X = 0.218
_L_FORE_Z = 0.057
_L_WRIST = 0.1447
_ARM_J_LIMITS = [
    (-2.61799, 2.61799), (0.0, 2.96706), (-2.87979, 0.0),
    (-1.51844, 1.51844), (-1.3439, 1.3439), (-2.79253, 2.79253),
]
# Limiti in gradi per UI: -360° a +360° per tutti i giunti
_ARM_J_LIMITS_DEG = [(-360.0, 360.0)] * 6
_ARM_J_NAMES = ["J1", "J2", "J3", "J4", "J5", "J6"]


def _arm_fk_points(j2, j3, j4):
    s23 = j2 + j3
    s234 = s23 + j4
    p0 = (0.0, 0.0)
    p1 = (-_L_UPPER * np.cos(j2), _L_UPPER * np.sin(j2))
    p2 = (
        p1[0] + _L_FORE_X * np.cos(s23) + _L_FORE_Z * np.sin(s23),
        p1[1] - _L_FORE_X * np.sin(s23) + _L_FORE_Z * np.cos(s23),
    )
    p3 = (
        p2[0] + _L_WRIST * np.cos(s234),
        p2[1] - _L_WRIST * np.sin(s234),
    )
    return [p0, p1, p2, p3]


def _arm_trackbar_to_rad(pos, i):
    """Trackbar mostra gradi (-360..+360) direttamente -> radianti."""
    return np.radians(float(pos))


def _arm_rad_to_trackbar(rad, i):
    """Radianti -> valore trackbar in gradi (-360..+360)."""
    deg = int(np.clip(np.degrees(rad), -360, 360))
    return deg


def _arm_trackbar_pos_to_deg(pos, i):
    """Posizione trackbar = gradi (per visualizzazione)."""
    return float(pos)


def _on_arm_trackbar(_):
    global arm_manual_ctrl
    try:
        manual = cv.getTrackbarPos("Manual", "Arm Motors")
        if manual <= 0:
            arm_manual_ctrl = None
            return
        vals = []
        for i in range(6):
            p = cv.getTrackbarPos(_ARM_J_NAMES[i], "Arm Motors")
            vals.append(_arm_trackbar_to_rad(p, i))
        arm_manual_ctrl = vals
    except Exception:
        pass


def _create_arm_trackbars(sensordata):
    global arm_trackbars_created
    if arm_trackbars_created:
        return
    arm_trackbars_created = True
    cv.namedWindow("Arm Motors")
    cv.createTrackbar("Manual", "Arm Motors", 0, 1, _on_arm_trackbar)
    for i in range(6):
        curr = float(sensordata[12 + i])
        init_deg = _arm_rad_to_trackbar(curr, i)
        cv.createTrackbar(_ARM_J_NAMES[i], "Arm Motors", init_deg, 720, _on_arm_trackbar)
        try:
            cv.setTrackbarMin(_ARM_J_NAMES[i], "Arm Motors", -360)
            cv.setTrackbarMax(_ARM_J_NAMES[i], "Arm Motors", 360)
            cv.setTrackbarPos(_ARM_J_NAMES[i], "Arm Motors", init_deg)
        except Exception:
            pass
    _on_arm_trackbar(0)


def render_arm_motors(sensordata):
    """Ritorna immagine BGR per la scheda Arm Motors (stile Body/Wrist Camera)."""
    joints = [float(sensordata[12 + i]) for i in range(6)]
    w, h = 340, 260
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (26, 26, 46)

    # Leggi target da trackbar se in manuale (per mostrare valore impostato)
    manual = 0
    try:
        manual = cv.getTrackbarPos("Manual", "Arm Motors")
    except Exception:
        pass

    # Barre giunti (range -360°..+360°)
    bar_h, bar_w = 14, 200
    lo_deg, hi_deg = -360.0, 360.0
    for i in range(6):
        deg_actual = np.degrees(joints[i])
        deg_display = deg_actual
        if manual > 0:
            try:
                p = cv.getTrackbarPos(_ARM_J_NAMES[i], "Arm Motors")
                deg_display = _arm_trackbar_pos_to_deg(p, i)  # mostra target (valore slider)
            except Exception:
                pass
        pct = (deg_actual - lo_deg) / (hi_deg - lo_deg) if hi_deg > lo_deg else 0.5
        pct = np.clip(pct, 0, 1)
        y = 28 + i * 36
        cv.rectangle(img, (90, y), (90 + bar_w, y + bar_h), (40, 40, 60), -1)
        cv.rectangle(img, (90, y), (90 + int(bar_w * pct), y + bar_h), (74, 158, 255), -1)
        cv.putText(img, _ARM_J_NAMES[i], (8, y + 12), cv.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)
        cv.putText(img, f"{deg_display:+.0f}°", (295, y + 12), cv.FONT_HERSHEY_SIMPLEX, 0.4, (100, 220, 255) if manual > 0 else (180, 180, 180), 1)

    # Vista 2D braccio
    j2, j3, j4 = joints[1], joints[2], joints[3]
    pts = _arm_fk_points(j2, j3, j4)
    scale = 120
    cx, cy = 50, h - 55
    px = [int(cx + p[0] * scale) for p in pts]
    pz = [int(cy - p[1] * scale) for p in pts]
    for k in range(len(px) - 1):
        cv.line(img, (px[k], pz[k]), (px[k + 1], pz[k + 1]), (74, 158, 255), 2)
    for k in range(len(px)):
        r = 5 if k < 3 else 6
        cv.circle(img, (px[k], pz[k]), r, (107, 179, 255), -1)
        cv.circle(img, (px[k], pz[k]), r, (255, 255, 255), 1)

    mode_txt = "MANUAL" if manual > 0 else "AUTO"
    cv.putText(img, f"Arm Motors [{mode_txt}]", (8, 18), cv.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

    # In modalità manuale: mostra target in gradi (trackbar già mostra -154/360)
    if manual > 0:
        try:
            targets = []
            for i in range(6):
                p = cv.getTrackbarPos(_ARM_J_NAMES[i], "Arm Motors")
                deg = _arm_trackbar_pos_to_deg(p, i)
                targets.append(f"{_ARM_J_NAMES[i]}:{deg:+.0f}°")
            cv.putText(img, "Target: " + "  ".join(targets), (8, h - 8),
                      cv.FONT_HERSHEY_SIMPLEX, 0.4, (100, 220, 255), 1)
        except Exception:
            pass
    return img


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
        _poll_wrist_cam_control()
        _apply_wrist_cam_orientation()
        locker.acquire()
        viewer.sync()

        bl = ball_in_robot_frame()
        rz = float(mj_data.xpos[mj_model.body("base_link").id][2]) if bl else 0.33
        grab_dist = 999.0
        if _ball_body_id >= 0:
            tip = _tool_tip_world()
            if tip is not None:
                grab_dist = float(
                    np.linalg.norm(tip - mj_data.xpos[_ball_body_id]))

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

        # Visualizzazione motori braccio (OpenCV): finestra "Arm Motors" con barre giunti / trackbar
        # manuali e anteprima posizioni. Disattivata sotto.
        # if config.ENABLE_DEPTH_CAMERA and num_motor_ >= 18:
        #     try:
        #         _create_arm_trackbars(mj_data.sensordata)
        #         arm_img = render_arm_motors(mj_data.sensordata)
        #         if arm_img is not None:
        #             cv.imshow("Arm Motors", arm_img)
        #     except Exception as e:
        #         pass

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

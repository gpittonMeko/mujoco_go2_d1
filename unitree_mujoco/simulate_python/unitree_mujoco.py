import time
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


locker = threading.Lock()

mj_model = mujoco.MjModel.from_xml_path(config.ROBOT_SCENE)
mj_data = mujoco.MjData(mj_model)

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

        locker.release()

        time_until_next_step = mj_model.opt.timestep - (
            time.perf_counter() - step_start
        )
        if time_until_next_step > 0:
            time.sleep(time_until_next_step)


def PhysicsViewerThread():
    global depth_image

    renderer = None
    if config.ENABLE_DEPTH_CAMERA:
        renderer = mujoco.Renderer(
            mj_model, config.CAMERA_ORIGINAL_HEIGHT, config.CAMERA_ORIGINAL_WIDTH
        )

    while viewer.is_running():
        step_start = time.perf_counter()
        locker.acquire()
        viewer.sync()

        if renderer is not None:
            try:
                renderer.update_scene(mj_data, camera=config.CAMERA_SENSOR_NAME)
                renderer.enable_depth_rendering()
                raw_depth = renderer.render()
                renderer.disable_depth_rendering()

                resized = cv.resize(
                    raw_depth,
                    (config.CAMERA_DOWNSAMPLED_WIDTH, config.CAMERA_DOWNSAMPLED_HEIGHT),
                    interpolation=cv.INTER_LINEAR
                )
                resized = np.clip(resized, config.NEAR_CLIP, config.FAR_CLIP)
                resized = (resized - config.NEAR_CLIP) / (config.FAR_CLIP - config.NEAR_CLIP)
                depth_image[:] = resized

                display = (255 * depth_image).astype(np.uint8)
                cv.imshow("Depth Camera", display)
                cv.waitKey(1)
            except Exception as e:
                print("Depth render error:", e)

        locker.release()
        time_until_next_step = config.VIEWER_DT - (time.perf_counter() - step_start)
        if time_until_next_step > 0:
            time.sleep(time_until_next_step)


if __name__ == "__main__":
    viewer_thread = Thread(target=PhysicsViewerThread)
    sim_thread = Thread(target=SimulationThread)

    viewer_thread.start()
    sim_thread.start()

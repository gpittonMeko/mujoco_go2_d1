ROBOT = "go2_d1" # Robot name, "go2", "b2", "b2w", "h1", "go2w", "g1", "go2_d1" (Go2+braccio Z1)
ROBOT_SCENE = "../unitree_robots/" + ROBOT + "/scene.xml" # Robot scene
DOMAIN_ID = 1 # Domain id
INTERFACE = "lan2"  # "lo" per sim locale (sudo ip link set lo multicast on). "lan2" se lo non funziona 

USE_JOYSTICK = 0 # Simulate Unitree WirelessController using a gamepad
JOYSTICK_TYPE = "xbox" # support "xbox" and "switch" gamepad layout
JOYSTICK_DEVICE = 0 # Joystick number

PRINT_SCENE_INFORMATION = True # Print link, joint and sensors information of robot
ENABLE_ELASTIC_BAND = False # Virtual spring band, used for lifting h1

SIMULATE_DT = 0.01   # Più alto = meno step/sec, sim in tempo reale su hardware lento
VIEWER_DT = 0.02  # 50 fps for viewer

ENABLE_DEPTH_CAMERA = True
CAMERA_SENSOR_NAME = "depth_camera"
CAMERA_ORIGINAL_WIDTH = 640
CAMERA_ORIGINAL_HEIGHT = 480
CAMERA_DOWNSAMPLED_WIDTH = 80
CAMERA_DOWNSAMPLED_HEIGHT = 60
NEAR_CLIP = 0.01
FAR_CLIP = 5.0
DEPTH_PUBLISH_DT = 0.05


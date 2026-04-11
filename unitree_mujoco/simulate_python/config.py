ROBOT = "go2_d1"  # Go2 + braccio Z1 (go2_d1.xml / scene.xml). Per D1 mesh: unitree_mujoco_d1viz.py + config_d1viz.
ROBOT_SCENE = "../unitree_robots/" + ROBOT + "/scene.xml" # Robot scene
DOMAIN_ID = 1 # Domain id
INTERFACE = "lo"  # "lo" per sim locale (sudo ip link set lo multicast on). Usa "lan2" solo se UP e collegata

USE_JOYSTICK = 0 # Simulate Unitree WirelessController using a gamepad
JOYSTICK_TYPE = "xbox" # support "xbox" and "switch" gamepad layout
JOYSTICK_DEVICE = 0 # Joystick number

PRINT_SCENE_INFORMATION = True # Print link, joint and sensors information of robot
ENABLE_ELASTIC_BAND = False # Virtual spring band, used for lifting h1

SIMULATE_DT = 0.01   # Più alto = meno step/sec, sim in tempo reale su hardware lento
VIEWER_DT = 0.02  # ~50 Hz: metà rispetto a 0.01 (100 Hz)

ENABLE_DEPTH_CAMERA = True
CAMERA_SENSOR_NAME = "depth_camera"
CAMERA_ORIGINAL_WIDTH = 640
CAMERA_ORIGINAL_HEIGHT = 480
CAMERA_DOWNSAMPLED_WIDTH = 80
CAMERA_DOWNSAMPLED_HEIGHT = 60
NEAR_CLIP = 0.01
FAR_CLIP = 5.0
DEPTH_PUBLISH_DT = 0.05

# Rilevamento oggetto rosso su RGB (body + wrist): più permissivo per mesh / luce non ideale
BALL_HSV_H1_MAX = 15
BALL_HSV_H2_MIN = 165
BALL_HSV_S_MIN = 45
BALL_HSV_V_MIN = 45
BALL_MIN_CONTOUR_AREA = 10
BALL_MORPH_OPEN_K = 3
BALL_MORPH_DILATE_K = 7
BALL_DEPTH_PATCH_RADIUS = 5


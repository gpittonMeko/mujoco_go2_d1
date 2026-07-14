# Riferimenti Unitree

- [H2 ROS2 / rete](https://support.unitree.com/home/en/H2_developer/ros2_communication_routine)
- [BrainCo Hand](https://support.unitree.com/home/en/G1_developer/brainco_hand)
- [brainco_hand_service](https://github.com/unitreerobotics/brainco_hand_service)
- [unitree_sdk2_python](https://github.com/unitreerobotics/unitree_sdk2_python)

Topic DDS usati dalla demo:

| Topic | Uso |
|-------|-----|
| `rt/arm_sdk` | Braccio sinistro |
| `rt/brainco/left/cmd` | Mano sinistra |
| `rt/lowstate` | Stato robot (lettura) |

Per estendere la demo: modificare `h2_demo_casoria.py` nel repo PC lab e fare deploy con `deploy_h2_demo_to_jetson.py`.

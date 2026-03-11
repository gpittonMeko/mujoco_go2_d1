# MuJoCo Go2 D1

Simulatore MuJoCo per **Unitree Go2** con depth camera, policy RL pre-addestrate e braccio Z1 (placeholder D1).

## Quick start

```bash
# Terminale 1: avvia simulatore (con depth camera OpenCV)
cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py

# Terminale 2: policy di camminata
python3 scripts/deploy_policy.py --model ts --vx 0.5
```

## Funzionalità

- **Simulatore MuJoCo** – unitree_mujoco con bridge DDS (stessi comandi del robot reale)
- **Depth camera** – rendering OpenCV, topic DDS `rt/depthimage`
- **Policy RL** – Teacher-Student e Walk-These-Ways (go2_deploy)
- **Joystick virtuale** – controllo da tastiera (`virtual_joystick/main.py`)
- **Go2 + braccio** – modello Go2 con braccio Z1 (placeholder D1), `ROBOT = "go2_d1"`

## Setup

1. **Dipendenze:** `unitree_sdk2_python`, `mujoco`, `cyclonedds`, `opencv-python`, `torch`, `pyyaml`
2. **CycloneDDS:** compilare da `cyclonedds/` se necessario
3. **Braccio (opzionale):** `python3 d1_arm/scripts/build_go2_arm.py`

## Struttura

```
├── unitree_mujoco/     # Simulatore + depth camera
├── unitree_sdk2_python/
├── cyclonedds/
├── go2_deploy/         # Modelli RL (wtw, ts)
├── scripts/            # deploy_policy.py, test_movimento.py
├── virtual_joystick/   # Joystick virtuale
├── d1_arm/             # Braccio D1/Z1: guida, montaggio, modello MuJoCo
└── docs/               # Documentazione completa
```

## Documentazione

Vedi **[docs/README.md](docs/README.md)** per setup, controllo, policy e troubleshooting.

## Licenza

Componenti da unitree_mujoco, unitree_sdk2, go2_deploy: rispettive licenze originali (BSD-3, ecc.).  
Codice aggiunto: BSD-3-Clause.

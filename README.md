# MuJoCo Go2 D1

Simulatore MuJoCo per **Unitree Go2** (depth camera, policy RL, braccio go2_d1).

## Quick start

```bash
cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py
# altro terminale:
python3 scripts/deploy_policy.py --model ts --vx 0.5
```

**Robot / Jetson NX:** `python3 scripts/serve_focus_dashboard.py` con `GO2_LOCAL=1` sulla porta **5056**. La `serve_dashboard_lite.py` è legacy su **5052**.

## Setup

Dipendenze tipiche: `unitree_sdk2_python`, `mujoco`, `cyclonedds`, `opencv-python`, `torch`, `pyyaml`. Opzionale braccio: `python3 d1_arm/scripts/build_go2_arm.py`.

## Struttura (estratto)

```
unitree_mujoco/   simulatore + depth
scripts/          policy, test, deploy NX
go2_dashboard/    factory HTTP + camere
templates/        UI dashboard
diagnostics_dashboard.py   API Flask (handlers)
docs/             indice in docs/README.md
```

## Documentazione

[indice docs/](docs/README.md)

## Licenza

Componenti upstream (unitree_mujoco, sdk, go2_deploy): licenze rispettive. Codice aggiunto: BSD-3-Clause.

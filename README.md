# MuJoCo Go2 D1

Simulatore MuJoCo per **Unitree Go2** (depth camera, policy RL, braccio go2_d1).

## Quick start

```bash
cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py
# altro terminale:
python3 scripts/deploy_policy.py --model ts --vx 0.5
```

**Robot / Jetson NX:** `python3 scripts/serve_dashboard_modular.py` con `GO2_LOCAL=1` — dettagli in [docs/localDogTest_jetson.md](docs/localDogTest_jetson.md).

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

**Handoff presa 6D (Sandy):** [docs/HANDOFF_SANDY.md](docs/HANDOFF_SANDY.md) — solo grasp, URL `http://192.168.123.18:5056/`. Se il collega non è sulla rete cane: `python scripts/proxy_d1_dashboard_lan.py` sul PC lab.

## Licenza

Componenti upstream (unitree_mujoco, sdk, go2_deploy): licenze rispettive. Codice aggiunto: BSD-3-Clause.

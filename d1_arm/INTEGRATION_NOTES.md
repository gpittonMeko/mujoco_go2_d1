# Note per integrazione D1 in simulazione

Appunti per chi vuole aggiungere il D1 al simulatore unitree_mujoco.

## Requisiti

- Modello MuJoCo del D1 (URDF → MJCF o MJCF nativo)
- Punto di attacco: `base_link` del Go2, posizione tipica payload ~(0.2, 0, 0.1) rispetto al centro
- 6 joint + 1 per gripper = 7 attuatori aggiuntivi

## Possibili fonti modello

- [Unitree D1-T](https://www.unitree.com/mobile/D1-T/) – specifiche tecniche
- Richiesta CAD a Unitree (support@unitree.cc)
- Modelli community per unitree_sim (vikashplus)

## Estensioni bridge

- Nuovi topic DDS per stato/comandi braccio (se Unitree ha un formato standard)
- Estensione di `unitree_sdk2py_bridge.py` per gestire messaggi D1

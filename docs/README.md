# Unitree Simulator – Documentazione

Guida unificata al progetto: setup, controllo Go2, policy RL, depth camera, troubleshooting.

---

## 1. Setup

**Dipendenze:** unitree_sdk2_python, mujoco, cyclonedds, opencv, pytorch, pyyaml

**Config** (`unitree_mujoco/simulate_python/config.py`): `ROBOT`, `DOMAIN_ID` (1=sim), `INTERFACE` (lo o lan2)

**Avvio simulatore (con depth camera):**
```bash
cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py
```
Si apre la finestra MuJoCo + finestra OpenCV "Depth Camera". Disabilitare con `ENABLE_DEPTH_CAMERA = False` in config.

---

## 2. Controllo Go2

Solo **low-level** (LowCmd). Sport Mode/Walk non disponibili in sim.

**12 motori:** FR(0-2), FL(3-5), RR(6-8), RL(9-11) – hip, thigh, calf  
**Parametri:** q (pos), dq (vel), kp, kd, tau

**Script base:**
- `python3 unitree_mujoco/example/python/stand_go2.py` – alzarsi/abbassarsi
- `python3 scripts/test_movimento.py` – stand up → squat → stand down

---

## 3. Policy di camminata RL (go2_deploy)

Integrazione delle policy pre-addestrate da [lupinjia/go2_deploy](https://github.com/lupinjia/go2_deploy) **senza compilare lo stack C++**: lo script `scripts/deploy_policy.py` carica i modelli PyTorch in Python e comunica via DDS con il simulatore (stessa architettura del robot reale).

**Modelli disponibili:**
- **Teacher-Student (ts)** – 45 obs, 20 frame di history, più stabile
- **Walk-These-Ways (wtw)** – 61 obs, gait parametrizzabile (altezza, foot clearance, pitch)

**Come usare:**
```bash
# Terminale 1: avvia il simulatore
cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py

# Terminale 2: policy Teacher-Student, avanti a 0.5 m/s
python3 scripts/deploy_policy.py --model ts --vx 0.5

# Go2_d1: braccio fisso in pose 0 (non influenza movimento)
python3 scripts/deploy_policy.py --model ts --vx 0.5 --arm-hold

# Joystick virtuale con braccio fisso
python3 virtual_joystick/main.py --arm-hold
```

**Parametri script:**
- `--model ts | wtw` – quale policy usare
- `--vx`, `--vy`, `--vyaw` – velocità desiderate (m/s, rad/s)
- `--interface enp3s0` – per robot reale (domain_id=0); se omesso usa l’interfaccia di config del simulatore

**Joystick virtuale:** per controllo da tastiera, usa `python3 virtual_joystick/main.py` (vedi `virtual_joystick/README.md`).

---

## 4. Depth Camera (da fork lupinjia/unitree_mujoco)

Integrazione depth camera + OpenCV per rendering in tempo reale e uso SLAM.

**Cosa è stato implementato:**
- **config.py** – variabili `ENABLE_DEPTH_CAMERA`, risoluzione (640×480 → 80×60 downsampled), `NEAR_CLIP` / `FAR_CLIP`, `DEPTH_PUBLISH_DT`
- **unitree_mujoco.py** – rendering depth con `mujoco.Renderer`, ridimensionamento con OpenCV, finestra "Depth Camera" con mappa di profondità normalizzata
- **image_publisher/** – modulo per pubblicare la depth image su DDS (topic `rt/depthimage`) per integrazioni esterne (es. SLAM)

**Modello robot:** in `go2.xml` è definita la camera `depth_camera`, FOV 62° (stile D435i), posizione/orientazione sul corpo.

**Config in config.py:**
- `ENABLE_DEPTH_CAMERA` – attiva/disattiva camera e finestra OpenCV
- `CAMERA_SENSOR_NAME` – nome della camera nel modello (default `depth_camera`)
- `CAMERA_ORIGINAL_WIDTH/HEIGHT` – risoluzione rendering (640×480)
- `CAMERA_DOWNSAMPLED_WIDTH/HEIGHT` – risoluzione pubblicata/visualizzata (80×60)
- `NEAR_CLIP`, `FAR_CLIP` – range depth (metri) per normalizzazione
- `DEPTH_PUBLISH_DT` – periodo di pubblicazione su DDS

---

## 5. Training e SLAM

**Training RL:** unitree_rl_lab (IsaacLab), legged_gym_go2  
**SLAM:** Usare depth camera simulata con ORB-SLAM/RTAB-Map. Ground truth da `rt/sportmodestate`.

---

## 6. Troubleshooting

| Problema | Soluzione |
|----------|-----------|
| `interface 'lo' not multicast-capable` | `sudo ip link set lo multicast on` |
| Policy resta "In attesa lowstate" | 1) Avvia prima il simulatore 2) `sudo ip link set lo multicast on` 3) In config: `INTERFACE = "lo"` oppure prova `--interface lan2` |
| Simulatore non risponde | Stesso DOMAIN_ID (1), avviare prima sim poi script |
| `keyframe index cannot be negative` | Verificare ROBOT_SCENE in config |

---

## Struttura progetto

```
Unitree_Simulator/
├── unitree_mujoco/     # Simulatore + depth camera + image_publisher
├── unitree_sdk2_python/
├── cyclonedds/
├── go2_deploy/         # Modelli RL pre-addestrati (wtw, ts)
├── scripts/            # deploy_policy.py, test_movimento.py
├── virtual_joystick/   # Joystick virtuale (tastiera) per controllo movimento
├── d1_arm/             # Braccio D1/Z1: guida, build (--light=10% peso), arm_control.py
└── docs/
```

---

## 7. Materiale didattico

- **[Comandare il robot (per studenti)](comandare_il_robot_per_studenti.md)** – flusso LowCmd/LowState, ordine di avvio, policy, joystick, braccio `go2_d1`, checklist laboratorio.

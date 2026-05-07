# Unitree D1 – Braccio robotico per Go2

Guida per ottenere, montare e controllare il **Unitree D1**, braccio servo a 6 assi + gripper progettato per il Go2.

---

## 1. Cos’è il D1

Il **D1** è un braccio robotico leggero (2.37 kg) pensato per il Go2:

| Specifica | Valore |
|----------|--------|
| **DoF** | 6 assi + gripper |
| **Payload** | 500 g |
| **Portata** | 550 mm (senza gripper), 670 mm (con gripper) |
| **Alimentazione** | 24 V, 2.5 A (max 5 A), 60 W |
| **Interfacce** | RJ45 (comunicazione), Type-C (debug seriale), DC 5.5-2.1 (alimentazione) |
| **Motori** | Bus servo con controllo di forza |
| **J1, J4, J6** | ±135° |
| **J2, J3, J5** | ±90° |

**D1-T** è la variante per teleoperazione (VR, ROS, embodied AI), con kit dual-arm (~8500 USD) o quad-arm (~16000 USD).

---

## 2. Dove acquistarlo

| Rivenditore | Link | Note |
|-------------|------|------|
| **Unitree** | [unitree.com](https://www.unitree.com) | Sito ufficiale |
| **RobotShop** | [robotshop.com](https://www.robotshop.com/products/unitree-go2-servo-robotic-arm-d1) | ~4000–5000 USD |
| **Generation Robots** | [generationrobots.com](https://www.generationrobots.com/en/404130-go2-servo-robotic-arm.html) | Europa |
| **Elektor** | [elektor.com](https://www.elektor.com/products/unitree-go2-d1-servo-robotic-arm) | |
| **C.R.Kennedy** | [survey.crkennedy.com.au](https://survey.crkennedy.com.au/products/utd1arm/unitree-d1-servo-arm-for-go2) | Australia |

Prezzi indicativi: **~4000–10000 USD** a seconda di rivenditore e regione.

---

## 3. Montaggio sul Go2

Il D1 è un accessorio payload che si monta sulla **piastra superiore** del Go2 tramite i punti di fissaggio standard.

**Compatibilità:** Go2 EDU, Go2 Air, Go2 Pro.

**Passi generali:**
1. Spegnere il robot e scollegare la batteria.
2. Rimuovere eventuali coperture dalla piastra payload.
3. Allineare il D1 ai fori di montaggio (consultare il manuale D1).
4. Fissare con le viti fornite.
5. Collegare alimentazione 24 V e cavo RJ45 alla porta payload del Go2.
6. Ricollegare la batteria e accendere.

**Nota:** Con payload (D1, LIDAR, docking) il Go2 non deve fare side rolling; il manuale vieta il roll laterale con dispositivi esterni sul dorso.

**Documentazione:** per dimensioni e istruzioni precise:
- [Unitree Support](https://unitree-docs.readthedocs.io/)
- [support.unitree.com](https://support.unitree.com)
- Email: support@unitree.cc

---

## 4. Controllo e SDK

- **Comunicazione:** RJ45 (Ethernet/DDS o protocollo proprietario).
- **unitree_sdk2_python:** supporta Go2; per il D1 va verificata la presenza di topic/API dedicati.
- **App Go2:** il braccio può essere controllato dall’app ufficiale dopo l’installazione; il telecomando fisico non gestisce il braccio.
- **Z1:** Unitree ha documentazione per il braccio Z1 ([dev-z1.unitree.com](https://dev-z1.unitree.com)); il D1 potrebbe usare interfacce simili.

Per dettagli sul controllo del D1, contattare Unitree o consultare la documentazione aggiornata.

---

## 5. Simulazione (MuJoCo)

**Modelli MuJoCo:**
- **`go2_d1.xml`:** braccio Z1 (menagerie), generato da `scripts/build_go2_arm.py`.
- **`go2_d1_d1mesh.xml`:** mesh Unitree D1 (`d1_550_description`), limiti giunti da datasheet (J0–J5 su `arm_joint1`–`arm_joint6`; la pinza datasheet non è un DoF separato nel MJCF). Gli STL usano path **relativi a `meshdir` (`assets/`)**: `../../../../d1_550_description/meshes/…` fino alla root del clone (niente path assoluti tipo `/home/lab/...`).

**Setup (Z1 / build):**
```bash
cd d1_arm
python3 scripts/build_go2_arm.py           # peso normale
python3 scripts/build_go2_arm.py --light  # peso braccio 10%
python3 scripts/build_go2_arm.py --weightless  # peso e inerzie braccio azzerati
```

Lo script clona [mujoco_menagerie](https://github.com/google-deepmind/mujoco_menagerie), copia gli asset Z1 e genera `unitree_mujoco/unitree_robots/go2_d1/`.

**Avvio simulatore:**
```bash
# Go2 + Z1 (config.py, ROBOT = "go2_d1")
cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py

# Go2 + mesh D1
cd unitree_mujoco/simulate_python && python3 unitree_mujoco_d1viz.py
```

**Interfaccia virtuale controllo braccio:**
```bash
python3 d1_arm/arm_control.py
```
GUI con 6 slider (J1–J6) e preset Home/Fold. Usa gli stessi comandi LowCmd di D1/Z1 reale ([D1Arm services](https://support.unitree.com/home/en/developer/D1Arm_services)); D1 e Z1 condividono il protocollo low-level motor.

**Muovere Go2 senza che il braccio destabilizzi:** usa `--arm-hold` per tenere il braccio fisso in pose 0:
```bash
python3 scripts/deploy_policy.py --model ts --vx 0.5 --arm-hold
python3 virtual_joystick/main.py --arm-hold
```

Vedi **MUJOCO_INTEGRATION.md** per dettagli e modifiche al modello.

---

## 6. Riferimenti

- [Unitree D1-T](https://www.unitree.com/mobile/D1-T/)
- [Unitree Docs](https://unitree-docs.readthedocs.io/)
- [Unitree Support](https://support.unitree.com)
- [unitree_mujoco](https://github.com/unitreerobotics/unitree_mujoco) – simulatore Go2

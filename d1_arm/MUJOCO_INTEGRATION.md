# Integrazione braccio D1/Z1 in MuJoCo – Go2 con arm

## Situazione modelli

| Modello | URDF/MJCF pubblico | Fonte |
|---------|--------------------|-------|
| **D1** | ❌ No | Unitree non pubblica URDF/MJCF per il D1 |
| **Z1** | ✅ Sì | [mujoco_menagerie](https://github.com/google-deepmind/mujoco_menagerie) (`unitree_z1`), derivato da [unitree_ros z1_description](https://github.com/unitreerobotics/unitree_ros) |
| **Go2** | ✅ Sì | `unitree_mujoco` (locale), anche in mujoco_menagerie (`unitree_go2`) |

**Strategia:** usare il **Z1** come placeholder per il D1 (stessa famiglia, 6-DOF). Quando Unitree rilascerà un modello D1, si potrà sostituire.

---

## Come ottenere i modelli

### 1. mujoco_menagerie (Z1)

```bash
cd /home/lab/Documents/Unitree_Simulator
git clone --depth 1 https://github.com/google-deepmind/mujoco_menagerie.git
```

Il braccio Z1 è in `mujoco_menagerie/unitree_z1/`:
- `z1.xml` – modello MJCF
- `assets/` – mesh STL (z1_Link00.stl … z1_Link06.stl)

### 2. unitree_ros (URDF Z1, opzionale)

```bash
git clone --depth 1 https://github.com/unitreerobotics/unitree_ros.git
# robots/z1_description/ – URDF (se serve conversione manuale)
```

---

## Modificare il Go2 per avere il braccio a bordo

### Idea

Il braccio va attaccato come **child di `base_link`** del Go2. In MuJoCo si fa inserendo il corpo radice del braccio (es. `link00` dello Z1) come `<body>` figlio di `base_link`.

### Posizione di montaggio

Sulla piastra payload del Go2, il braccio è tipicamente sopra il dorso, leggermente avanti. Nel frame del corpo:
- `pos="0.15 0 0.06"` – centro piastra, ~6 cm sopra la base
- `euler="0 0 0"` – orientamento standard (asse del primo joint verso l’alto)

### Passi manuali

1. **Copiare gli asset Z1** nella cartella del Go2:
   ```bash
   mkdir -p unitree_mujoco/unitree_robots/go2/assets/arm
   cp mujoco_menagerie/unitree_z1/assets/*.stl unitree_mujoco/unitree_robots/go2/assets/arm/
   ```

2. **Creare `go2_with_arm.xml`** (o usare lo script `scripts/build_go2_arm.py`):
   - Includere `go2.xml`
   - Aggiungere in `<asset>` i mesh dell’arm con path `arm/z1_Link00.stl` ecc.
   - Inserire il corpo del braccio come child di `base_link` (vedi sotto).

3. **Struttura del braccio da inserire** (esempio con Z1):
   ```xml
   <!-- Dentro base_link, dopo le gambe -->
   <body name="arm_base" pos="0.15 0 0.06" childclass="z1">
     <!-- contenuto di link00 dello Z1: inertial, geom, poi link01...link06 -->
   </body>
   ```

4. **Attuatori:** aggiungere gli attuatori `motor1`…`motor6` dello Z1 nella sezione `<actuator>`.

5. **Sensori (opzionale):** jointpos/jointvel per i 6 joint del braccio.

### Uso dello script automatico

```bash
cd d1_arm
python3 scripts/build_go2_arm.py          # peso normale
python3 scripts/build_go2_arm.py --light  # peso braccio 10%
```

Lo script:
- Clona mujoco_menagerie se manca
- Copia gli asset Z1 in `unitree_robots/go2_d1/assets/arm/`
- Genera `go2_d1.xml` e `scene.xml` (Go2 + braccio, 18 attuatori: 12 gambe + 6 braccio)

**Avvio:** in `config.py` imposta `ROBOT = "go2_d1"` e lancia il simulatore.

---

## Modello D1 vs Z1 (per adattamenti futuri)

| | D1 | Z1 |
|---|-----|-----|
| Peso | 2.37 kg | ~4.5 kg |
| Portata | 550–670 mm | ~740 mm |
| Payload | 500 g | 2–3 kg |
| J1, J4, J6 | ±135° | Z1 ha range diversi |
| J2, J3, J5 | ±90° | |

Per un modello D1 accurato servirebbero:
- CAD o specifiche da Unitree
- Creazione MJCF da zero o adattamento dello Z1 (scale, range, inerzie)

---

## Riferimenti

- [mujoco_menagerie unitree_z1](https://github.com/google-deepmind/mujoco_menagerie/tree/main/unitree_z1)
- [mujoco_menagerie unitree_go2](https://github.com/google-deepmind/mujoco_menagerie/tree/main/unitree_go2)
- [unitree_ros](https://github.com/unitreerobotics/unitree_ros) – `robots/aliengoZ1_description` (Aliengo + Z1)
- [unitree_mujoco](https://github.com/unitreerobotics/unitree_mujoco)

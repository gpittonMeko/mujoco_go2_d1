# localDogTest — Dashboard Jetson e presa braccio D1

Questo ramo concentra la dashboard locale sul robot (`GO2_LOCAL=1`), il planner AprilTag / IK (`scripts/box_grasp_planner.py`) e gli helper C++ per comandi DDS sul braccio **Unitree D1**.

## Assunzione operativa (primo ciclo)

- Validazione **IK, ricerca sul polso e presa** con il cane **seduto o comunque fermo** (base stabile, niente passeggiata mentre si chiude il loop di controllo).
- **Non** si integra in questa fase il comportamento di **search/move** del corpo (Sport Mode, avvicinamento con le gambe). Quello è previsto dopo che presa e IK sono ripetibili da fermo.

## Disposizione AprilTag sul robot (riferimento spaziale)

Nel layout previsto sul stack Jetson / Go2:

- Il tag **tag25h9 ID 5** è montato **in corrispondenza del piano sopra il LiDAR XT-16** (landmark fisso sul corpo del robot), taglio fisico **61 mm**. Serve per orientarsi: davanti al LiDAR ci sono le telecamere; più avanti ancora, nella scena, l’oggetto da manipolare / punto di arresto. In pratica il **5** è spesso visibile **solo dalla wrist cam** (`/dev/video0`), non dal RealSense frontale. Per **confrontare** quando entrambe lo vedono: `GET /api/arm/tag5_calibration?dual_probe=1` (metriche `heuristic_disagreement_mm` / `calibrated_disagreement_mm`).
- I tag **0–3** sulla scatola hanno taglio **19 mm** (default nel planner). Nel codice (`scripts/box_grasp_planner.py`) le pose usano il lato corretto per ID; il tag **5** non entra nella media IK della presa.

## Dipendenze Python (Jetson)

Installare sul PC del robot (o venv) almeno: `flask`, `opencv-python`, `numpy`, `paramiko` (se la dashboard usa ancora SSH verso host remoti). Il planner importa `scripts/arm_kinematics_d1_template.py`.

### NX / Jetson: non serve Cyclone DDS Python per il braccio

Sul Jetson NX **`cyclonedds` via pip** è stato problematico (build/import/crash). Il controllo **braccio D1** non usa Python DDS: la dashboard invoca **`bin/d1_arm_command`** e **`bin/d1_arm_feedback_helper`**, compilati contro lo **Unitree SDK2** (che usa le librerie DDS/C Iceoryx lato sistema). Non è richiesto `pip install cyclonedds` per muovere il braccio con questo flusso.

Il modulo Python `cyclonedds` resta utile solo se vuoi usare **`unitree_sdk2py`** in modo nativo (es. probe `dds_lowstate` nella dashboard): quello può fallire senza `cyclonedds` Python — è diagnostico opzionale, **non** necessario per `publish_d1_arm_plan` né per gli helper C++.

## Variabili d’ambiente rilevanti

| Variabile | Ruolo |
|-----------|--------|
| `GO2_LOCAL` | `1` = cache camere/LiDAR sullo stesso host che esegue Flask |
| `GO2_HOST`, … | Host/parametri di rete diagnostici |
| `BOX_TAG_SIZE_M` | Lato fisico tag scatola **0–3** in metri (default **0.019** = 19 mm) |
| `REFERENCE_TAG_SIZE_M` | Lato fisico landmark **ID 5** sopra XT16 (default **0.061** = 61 mm). Alias: `LIDAR_LANDMARK_TAG_SIZE_M` |
| `GO2_ENABLE_REAL_ARM` | `1` per permettere movimenti reali via DDS (`publish_d1_arm_plan`) |
| `GO2_ENABLE_BASE_MOTION` | `1` sulla NX per consentire Sport RPC: modalità «Accompagna» (BalanceStand + SwitchJoystick → telecomando RC) |
| `GO2_ACCOMPANY_SPEED_LEVEL` | Livello velocità Sport prima del joystick (default `1`) |
| `GO2_DDS_DOMAIN`, `GO2_DDS_INTERFACE` | Dominio DDS e interfaccia (vuota se default) |
| `D1_SEARCH_DELAY_MS` | Pausa tra messaggi DDS durante ricerca braccio (default più alto = più fluido) |
| `D1_PLAN_DELAY_MS` | Pausa durante esecuzione IK presa/solleva (default ~620 ms) |
| `D1_SEARCH_MAX_CYCLES` | Cicli ricerca polso prima di arrendersi (default 10) |
| `D1_MAX_STEP_DEG_SEARCH` / `D1_MAX_STEP_DEG_GRASP` | Sette numeri separati da virgola: max ° per giunto per interpolazione |
| `GO2_FRONT_CAMERA_FALLBACK_GRASP` | `1` = dopo i cicli, tenta presa da IK solo RealSense se il polso non ha mai lock (rischio collisioni) |
| `GO2_DASHBOARD_PORT` | Porta HTTP (default `5050`) |

## Helper DDS (binari)

Gli eseguibili previsti sotto la root del progetto:

- `bin/d1_arm_command` — pubblica JSON righe su `stdin` verso `rt/arm_Command`.
- `bin/d1_arm_feedback_helper` — legge feedback servo per interpolazione sicura.

I sorgenti sono in `scripts/d1_arm_dds_helper.cpp` e `scripts/d1_arm_feedback_helper.cpp`. Compilare sul Jetson con lo script [`scripts/build_d1_arm_helpers.sh`](../scripts/build_d1_arm_helpers.sh). Serve toolchain **g++**, **Unitree SDK2** installato sul sistema target e le sue librerie (link `-lddsc`/Iceoryx secondo la distro robot — **non** il wheel pip `cyclonedds`).

La cartella `bin/` è in `.gitignore`: i binari non si versionano.

## Sequenza operativa sul NX

1. Compilare i due helper in `bin/` sul Jetson.

La dashboard **non gira sul PC di sviluppo**: Flask + OpenCV + DDS helper girano **solo sulla Jetson NX** (`GO2_LOCAL=1`), perché camere USB e `bin/d1_arm_*` sono sul robot.

2. Dal repository sul PC collegato alla LAN `192.168.123.x` eseguire:

```bash
python scripts/deploy_dashboard_to_nx.py
```

Carica lo starter sulla NX (script LF Unix), imposta `GO2_LOCAL=1`, `GO2_ENABLE_REAL_ARM=1`, avvia Flask e verifica HTTP sulla macchina.

3. Nel browser sul PC: **`http://192.168.123.18:5050/`** (porta default `GO2_DASHBOARD_PORT`).

4. **Allineamento START (opzionale):** avvia camere dalla dashboard. **Salva START (AprilTag + arm)** scrive `data/start_alignment.json` con snapshot `/api/box/plan` **e** `arm_at_start` (angoli servo + `joints_rad` per seed IK se `d1_arm_feedback_helper` risponde). Vedi [`docs/d1_arm_protocol_feasibility.md`](../docs/d1_arm_protocol_feasibility.md) per accompagnamento passivo tipo UR (non ancora implementato via DDS finché manca la tabella protocollo). Per Stand up / Crouch sulla base usare la sezione opzionale «Base quadrupede» nella dashboard. `GO2_ENABLE_BASE_MOTION=1` serve solo per quei comandi Sport.

5. Test presa (operatore attento): `POST /api/arm/grasp_box/attempt` o pulsante nell’interfaccia.

Controllare nella risposta JSON `scan_hints`, lock sul polso (`/dev/video0`) e motivi di sicurezza se il feedback servo non è disponibile.

## Checklist test presa (robot reale)

- [ ] Helper presenti e eseguibili in `bin/`
- [ ] `GO2_ENABLE_REAL_ARM=1` solo quando si intende muovere il braccio
- [ ] Frame freschi da `/dev/video0` (polso) e `/dev/video6` (RealSense RGB)
- [ ] Cane **fermo**; area libera davanti al gripper
- [ ] Opzionale: `GO2_ENABLE_BASE_MOTION=1` e SDK Python DDS OK se usi accompagna RC
- [ ] Posizione START salvata in `data/start_alignment.json` quando serve ripetibilità scena

## Roadmap successiva

Dopo presa ripetibile da fermo: integrazione locomotion/search base e rivalutazione della calibrazione camera→base in movimento.

# localDogTest — Dashboard Jetson e presa braccio D1

Questo ramo concentra la dashboard locale sul robot (`GO2_LOCAL=1`), il planner AprilTag / IK (`scripts/box_grasp_planner.py`) e gli helper C++ per comandi DDS sul braccio **Unitree D1**.

## Assunzione operativa (primo ciclo)

- Validazione **IK, ricerca sul polso e presa** con il cane **seduto o comunque fermo** (base stabile, niente passeggiata mentre si chiude il loop di controllo).
- **Non** si integra in questa fase il comportamento di **search/move** del corpo (Sport Mode, avvicinamento con le gambe). Quello è previsto dopo che presa e IK sono ripetibili da fermo.

## Dipendenze Python (Jetson)

Installare sul PC del robot (o venv) almeno: `flask`, `opencv-python`, `numpy`, `paramiko` (se la dashboard usa ancora SSH verso host remoti). Il planner importa `scripts/arm_kinematics_d1_template.py`.

## Variabili d’ambiente rilevanti

| Variabile | Ruolo |
|-----------|--------|
| `GO2_LOCAL` | `1` = cache camere/LiDAR sullo stesso host che esegue Flask |
| `GO2_HOST`, … | Host/parametri di rete diagnostici |
| `GO2_ENABLE_REAL_ARM` | `1` per permettere movimenti reali via DDS (`publish_d1_arm_plan`) |
| `GO2_DDS_DOMAIN`, `GO2_DDS_INTERFACE` | Dominio DDS e interfaccia (vuota se default) |
| `D1_SEARCH_*`, `D1_SEARCH_DELAY_MS` | Ricerca guidata dalla RGB frontale (nominali e tempo tra messaggi) |
| `GO2_DASHBOARD_PORT` | Porta HTTP (default `5050`) |

## Helper DDS (binari)

Gli eseguibili previsti sotto la root del progetto:

- `bin/d1_arm_command` — pubblica JSON righe su `stdin` verso `rt/arm_Command`.
- `bin/d1_arm_feedback_helper` — legge feedback servo per interpolazione sicura.

I sorgenti sono in `scripts/d1_arm_dds_helper.cpp` e `scripts/d1_arm_feedback_helper.cpp`. Compilare sul Jetson con lo script [`scripts/build_d1_arm_helpers.sh`](../scripts/build_d1_arm_helpers.sh) (richiede Unitree SDK2 + dipendenze Cyclone/Iceoryx come sul sistema target).

La cartella `bin/` è in `.gitignore`: i binari non si versionano.

## Sequenza operativa sul NX

1. Compilare i due helper in `bin/`.
2. Esportare le variabili necessarie (`GO2_LOCAL=1`, DDS, ecc.).
3. Avviare: `python3 diagnostics_dashboard.py` dalla root del repo.
4. Test presa (con braccio abilitato e operatore attento): `POST /api/arm/grasp_box/attempt` oppure pulsante equivalente nell’interfaccia.

Controllare nella risposta JSON `scan_hints`, lock sul polso (`/dev/video0`) e motivi di sicurezza se il feedback servo non è disponibile.

## Checklist test presa (robot reale)

- [ ] Helper presenti e eseguibili in `bin/`
- [ ] `GO2_ENABLE_REAL_ARM=1` solo quando si intende muovere il braccio
- [ ] Frame freschi da `/dev/video0` (polso) e `/dev/video6` (RealSense RGB)
- [ ] Cane **fermo**; area libera davanti al gripper
- [ ] Osservare prima pianificazione dry-run da `/api/box/plan`, poi tentativo grasp

## Roadmap successiva

Dopo presa ripetibile da fermo: integrazione locomotion/search base e rivalutazione della calibrazione camera→base in movimento.

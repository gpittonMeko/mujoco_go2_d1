# Handoff a SandyTurchetto — presa 6D D1

Documento di consegna: struttura utile, porte, deploy e regole di sicurezza.
Per il flusso curl dettagliato vedi anche [D1_GRASP6D_NX.md](D1_GRASP6D_NX.md).

## Focus dell’attività (cosa ti chiediamo)

**Obiettivo:** far sì che la presa 6D dal polso (D456) vada **al centro dell’oggetto** in modo ripetibile, non solo “nella zona giusta”.

Stato lab oggi:
- Pipeline end-to-end **funziona**: SCAN → preview → execute / ciclo pick–drop.
- **Orientamento** della pinza (top-down / lato corto) spesso è **ok**.
- Problema ricorrente: a ogni presa c’è un **offset spaziale** — un po’ di lato, un po’ sopra, un po’ sotto. Non sembra un errore di yaw/pitch grossolano, ma uno **shift di posizione** (traslazione) rispetto al target.

Ipotesi da verificare (in ordine tipico):

1. **Qualità hand-eye** — residual alto / campioni poco diversificati → errore sistematico in XYZ.
2. **Offset TCP** (`D1_GRASP6D_TCP_*`) — oggi Y è stato alzato a ~50 mm in modo empirico per centrare la chiusura; può mascherare o spostare l’errore.
3. **Bias Z / contatto** (`D1_GRASP6D_FORCE_BIAS_Z_M`, stop-short) — alzano/abbassano il contatto senza “curare” il laterale.
4. **Stima oggetto** (depth cluster vs footprint RGB) — centro geometrico sbagliato a volte, mentre l’approccio resta angolarmente coerente.

Non è “riscrivere tutto il 6D”: è **diagnosticare e chiudere l’offset** (calibrazione + TCP + eventuale correzione residuale), con prove ripetibili e misure (preview vs moto reale, debug.jpg).

## Come funziona la calibrazione adesso

Non è più solo “ArUco ID 0 a mano”. Flusso attuale sul jog **5056**:

1. **AprilGrid** sul tavolo (lab: first ID tipicamente **312**, griglia 6×4, tag ~30 mm, gap ~15 mm — vedi env). Griglia **libera** (niente cuboidi sopra) durante la calibrazione.
2. Braccio in **controllo** (couple + HOLD). Modalità UI **A · Calibra**.
3. **Sample** da pose diverse (manuale: HOLD → sample ripetuto; oppure **AUTO** se `D1_GRASP6D_AUTO_MOTION_ENABLE=1`): ogni sample salva posa tool + osservazione marker.
4. **Build** hand-eye: `POST /api/pick/metric/calibration/build` → file calibrazione su NX (`data/d1_grasp6d_*.json`, **non** in git).
5. Controllare residual / quality (UI o API calibration). Residual alti → non fidarsi del pick “al millimetro”.
6. Solo dopo calib `ok`: rimettere l’oggetto, **B · Prendi**, `preview` poi `execute` / ciclo.

Nota: la doc curl in `D1_GRASP6D_NX.md` descrive ancora il percorso sample/build generico; in lab si usa soprattutto il wizard Teach + AprilGrid 312. Gli oggetti di presa **dopo** la calib, non sopra la griglia.

File robot-specifici restano sulla NX: ricalibrare lì se sospetti hand-eye; non aspettarti che il clone git porti una calib “buona”.

## Problema noto: offset ricorrente (lato / sopra / sotto)

Sintomo tipico osservato in sessione:
- La pinza arriva con **angolazione plausibile**.
- Il punto di contatto/chiusura è **spostato**: laterale (spesso asse chiusura / TCP Y), e/o in altezza (troppo alto → manca; troppo basso → tuffo).
- Si è già intervenuti con **patch empiriche** (TCP Y ≈ 50 mm, bias Z, chiusura ferma, ciclo drop dall’alto). Hanno **mitigato** casi specifici; **non** dimostrano che la catena camera→base sia corretta.

Come investigare (consigliato):

| Step | Cosa fare | Cosa leggere |
|------|-----------|--------------|
| 1 | Ricalibrare hand-eye con molti sample e viste diverse; confrontare residual prima/dopo | `/api/pick/metric/calibration`, quality/residual |
| 2 | Fissare un oggetto noto; `preview` e annotare XYZ target vs dove va davvero il TCP | `debug.jpg`, log execute |
| 3 | Se l’errore è **costante** in un asse → sospetta TCP / frame flange–gripper | `D1_GRASP6D_TCP_X/Y/Z_M` in `nx_d1_jog_env.sh` |
| 4 | Se l’errore **cambia** con posa/oggetto → sospetta hand-eye o stima cluster/RGB | `grasp6d.py` path cluster vs RGB-guided |
| 5 | Evitare di “aggiustare a caso” tre knobs insieme: un asse alla volta, 3–5 prove ripetute | — |

Deliverable atteso: offset residuale piccolo e **stabile**, con calib + TCP documentati (valori finali e perché), non solo un env che “a occhio prende”.

## Cosa conta (e cosa no)

| Area | Path / comando | Note |
|------|----------------|------|
| App jog + grasp 6D | `go2_dashboard/d1_jog/` | Core: `app.py`, `grasp6d.py`, `wrist_rgbd.py`, `pick_preset.py` |
| Serve UI/API D1 | `scripts/serve_d1_jog_dashboard.py` | Porta **5056** |
| Env NX D1 | `scripts/nx_d1_jog_env.sh` | TCP, bias, gripper, RealSense |
| Deploy D1 (5056) | `scripts/deploy_d1_jog_to_nx.py` | **Questo** per grasp/jog |
| Deploy operator | `scripts/deploy_dashboard_to_nx.py` | Porta tipica **5050** — non confondere |
| Hold daemon | `scripts/d1_hold_daemon.py` + `nx_d1_hold_supervise.sh` | Hold esterno, indipendente da Flask |
| Doc operativa 6D | `docs/D1_GRASP6D_NX.md` | Sequenza calib / preview / execute |
| Indice docs | `docs/README.md` | |

Fuori scope handoff 6D: sim MuJoCo (`unitree_mujoco/`), `cyclonedds/`, `old/`, dump jpg/json in root (gitignored).

## Porte

- **5056** — dashboard D1 jog + grasp 6D (`http://192.168.123.18:5056/`)
- **5050** — dashboard operator (modulo generale); non è il percorso grasp polso

## Deploy e sicurezza braccio

1. Portare il braccio in **folded / safe transit**:  
   `POST /api/arm/true_zero` body `{"op":"goto_zero"}`  
   (J1≈−90°, J2≈+90°, J0 allineato allo yaw di scan — **non** il funcode-7 `/api/joints/zero`).
2. Conferma umana che è davvero folded.
3. Restart solo con flag espliciti, da PC in rete NX:

```powershell
$env:GO2_D1_JOG_RESTART='1'
$env:GO2_D1_CONFIRM_ARM_SUPPORTED='1'
$env:D1_JOG_SKIP_SDK_BUILD='1'
$env:GO2_D1_RELOAD_HOLD_DAEMON='0'
python scripts/deploy_d1_jog_to_nx.py
```

Default del deploy D1: **solo copia file** (nessun restart). Dopo il restart verificare `hold_active`.

Regola Cursor: `.cursor/rules/nx-jetson-dashboard-deploy.mdc`.

## Env critici (stato lab attuale)

Definiti in `scripts/nx_d1_jog_env.sh` (override possibili su NX):

| Variabile | Ruolo |
|-----------|--------|
| `D1_GRASP6D_TCP_Y_M` | Offset asse chiusura TCP (lab ~`0.050`) |
| `D1_GRASP6D_TCP_X_M` / `_Z_M` | Offset TCP restanti |
| `D1_GRASP6D_FIRM_CLOSE_MAX_DEG` | Chiusura pinza ferma (evita J6 “mezzo aperto”) |
| `D1_GRASP6D_GRIP_COMPRESSION_M` | Compressione grasp vs larghezza |
| `D1_GRASP6D_FORCE_BIAS_Z_M` | Bias Z contatto (anti-tuffo / altezza) |
| `D1_GRASP6D_CONTACT_STOP_SHORT_M` | Arresto anticipato sull’approccio (0 = pieno contatto) |
| `D1_GRASP6D_DROP_EXTRA_UP_M` | Extra alzata prima del rilascio in ciclo |
| `D1_GRASP6D_APRILGRID_FIRST_ID` | Lab foglio: tipicamente `312` (tag 312–335) |
| `D1_GRASP6D_PREFER_RGB_GUIDED` | Preferenza footprint RGB se dims pack-like |

Calibrazioni/tuning **robot-specifiche** su NX non si sovrascrivono dal repo se già presenti (`data/d1_grasp6d_*.json` in `.gitignore`).

## Flusso grasp / ciclo

1. SCAN allineata: `POST /api/pick/grasp6d/scan_pose` `{"action":"goto"}`
2. Preview senza moto cieco: `POST /api/pick/grasp6d/preview`
3. Presa singola: `POST /api/pick/grasp6d/execute` con `confirm: "EXECUTE_GRASP6D"`
4. Ciclo pick → lift → **drop dall’alto** → ritorno SCAN:  
   `POST /api/pick/grasp6d/cycle/start` con `confirm: "RUN_GRASP6D_PICK_DROP_LOOP"`, `cycles: N`  
   Stop: `POST /api/pick/grasp6d/cycle/stop`

Debug overlay: `/api/pick/grasp6d/debug.jpg` (UI 5056: Diagnostica 6D).

## Branch di lavoro

- Consegna su `main` (stato pulito handoff).
- Branch feature: `_sandy_fix_6d_grasping` (parte dal tip di consegna).

## Checklist primo giorno

1. Clone / pull `main` o `_sandy_fix_6d_grasping`.
2. Leggere questo file (soprattutto **Focus**, **Calibrazione**, **Offset**) + `docs/D1_GRASP6D_NX.md`.
3. Verificare rete verso `192.168.123.18` e UI `http://192.168.123.18:5056/`.
4. Controllare `hold_active` e posa folded prima di ogni restart 5056.
5. Snapshot calib attuale su NX (ok? residual? sample_count?) prima di toccare TCP.
6. Non committare jpg/json di sessione in root (già in `.gitignore`).

# Handoff a SandyTurchetto — presa 6D D1

Documento di consegna: struttura utile, porte, deploy e regole di sicurezza.
Per il flusso curl dettagliato vedi anche [D1_GRASP6D_NX.md](D1_GRASP6D_NX.md).

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
2. Leggere questo file + `docs/D1_GRASP6D_NX.md`.
3. Verificare rete verso `192.168.123.18` e UI `http://192.168.123.18:5056/`.
4. Controllare `hold_active` e posa folded prima di ogni restart 5056.
5. Non committare jpg/json di sessione in root (già in `.gitignore`).

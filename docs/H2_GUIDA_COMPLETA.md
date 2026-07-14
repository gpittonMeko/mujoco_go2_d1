# H2 Demo — guida completa (architettura, codice, laboratorio)

Documento unico che descrive **come è stato costruito** il demo sul robot Unitree **H2** in laboratorio.

**Operatori:** leggere prima `00_ACCENSIONE_ACCESSI_E_STATO_ROBOT.md` (accensione, Regular Mode 1/2, password, BrainCo).

Per un riepilogo rapido vedi anche `01_QUICK_START.md`.

---

## 1. Obiettivo del demo

Sequenza **demo** eseguita dalla **Jetson Thor** (`.163`), con robot in piedi sul **protection frame**, locomozione in modalità **`ai`**:

1. **TTS** italiano — «Pellegrino Casoria di Accenture…» (`pellegrino_tts.wav`)
2. In parallelo con il meme audio **Cyberpunk** (~16 s):
   - braccio **sinistro** che sale lentamente (`rt/arm_sdk`)
   - mano **sinistra** BrainCo che si chiude (~72 %)
3. Ritorno braccio e apertura mano

Vincoli di sicurezza rispettati:

- Nessun `MotionSwitcher.ReleaseMode()`
- Nessun comando **Damp** con protection frame montato
- Controllo mani **solo via DDS** dalla Thor (mai SSH sulla seriale mani dalla Thor)
- Step che muovono il braccio richiedono `--confirm-arm` dal PC operatore

---

## 2. Architettura hardware e rete

### 2.1 Macchine sulla LAN `192.168.123.0/24`

| Host | IP | SSH | Credenziali | Ruolo |
|------|-----|-----|-------------|--------|
| PC1 locomozione | `.161` | chiuso | — | FSM, `rt/lowstate`, controllo gambe — **non accessibile** |
| **PC2 interno Unitree** | **`.162`** | solo personale lab | `unitree` / `Unitree#24226` | Seriale mani — **non è postazione operatore** |
| **Jetson Thor** | **`.163`** | aperto (demo) | `unitree` / **`123`** | Esecuzione demo DDS su **`eth10`** |
| PC laboratorio | `.50` (tipico) | — | — | Deploy, `h2_smoke_remote.py`, avvio BrainCo |

### 2.2 Flusso dati (diagramma)

```
[Mani BrainCo RS-485]
        │
        ▼
PC2 .162  /dev/ttyUN0 (destra → ttyACM0)
          /dev/ttyUN1 (sinistra → ttyACM2)
        │
        ▼
brainco_hand_server -n eth0   (~/brainco_hand_service/bin/)
        │
        ▼ DDS Cyclone
rt/brainco/left|right/{cmd,state}
        │
        ▼ eth10 (192.168.123.x)
Jetson Thor .163
  ├─ rt/arm_sdk          → braccio sinistro
  ├─ AudioClient (G1 API) → TTS + WAV
  └─ rt/brainco/left/cmd → presa mano sinistra
```

### 2.3 Cosa NON fare

| Errore | Perché |
|--------|--------|
| Installare `brainco_hand_server` sulla Thor | La Thor **non ha** seriale mani |
| SSH libero sul PC2 `.162` | **PC interno Unitree** — usare `h2_start_brainco_pc2.py` dal PC lab |
| `ReleaseMode()` o Damp sul frame | Rischio caduta / comportamento imprevedibile |
| Demo in Regular Mode 2 | Gesti braccio dal telecomando possono interferire |
| Password `123` o `Unitree0408` sul PC2 | Su questo H2 la password PC2 è **`Unitree#24226`** |

### 2.4 Porte seriali mani (H2 vs G1)

Sull’H2 le mani non sono `ttyUSB*`. Udev crea symlink:

- `/dev/ttyUN0` — mano destra (→ `ttyACM0`)
- `/dev/ttyUN1` — mano sinistra (→ `ttyACM2`)

Regola tipica: `99-unitree-sc-port.rules` sul PC2.

---

## 3. Come abbiamo configurato il laboratorio

### 3.1 Scoperta rete e credenziali

1. Ping e SSH verso `.163` (Thor) con password `123`.
2. Tentativi SSH verso `.162` con password errate (`123`, `Unitree0408`) → falliti.
3. Password corretta PC2: **`Unitree#24226`** (documentata anche in articoli community Unitree B2).
4. Verifica che la Thor vede `rt/lowstate` su `eth10` ma **non** `rt/brainco/*` finché il server mani non gira sul PC2.

### 3.2 PC2 — bridge mani (PC interno Unitree)

> **Il PC2 non è a accesso libero.** È il computer industriale interno Unitree che gestisce la seriale delle mani. Gli operatori demo **non** devono collegarsi in SSH al `.162`; usare lo script dal PC lab (sezione 3.2.2).

Il pacchetto **`brainco_hand_service`** è installato su PC2 in:

```text
/home/unitree/brainco_hand_service/bin/brainco_hand_server
```

ma **non partiva** all’avvio (nessun systemd attivo).

**Avvio manuale** (solo personale lab autorizzato sul PC2):

```bash
ssh unitree@192.168.123.162   # password Unitree#24226
cd ~/brainco_hand_service/bin
sudo ./brainco_hand_server -n eth0
```

#### 3.2.2 Avvio dal PC lab (metodo consigliato per la demo)

```powershell
python scripts/h2_start_brainco_pc2.py
```

Lo script si collega al PC2, riavvia `brainco_hand_server`, verifica DDS dalla Thor. **Obbligatorio dopo ogni reboot del PC2.**

**Nota:** non abbiamo modificato in profondità la `.162`. Opzionale: `setup_autostart.sh` per `brainco_hand.service` dopo conferma Unitree/operatore.

### 3.3 Jetson Thor — ambiente `~/h2_demo`

Directory di lavoro sulla Thor:

```text
/home/unitree/h2_demo/
├── README.md                 ← copia da docs/h2_demo_jetson_README.md
├── install_sdk.sh            ← CycloneDDS in /usr/local + pip sdk
├── scripts/                  ← script demo (senza h2_smoke_remote.py)
├── data/audio/
│   ├── pellegrino_tts.wav
│   └── cyberpunk_meme.wav
├── unitree_sdk2_python/      ← SDK Python clonato dal repo
└── offline/                  ← sorgente CycloneDDS per build
```

Variabili usate su Thor:

```bash
export CYCLONEDDS_HOME=/usr/local
export LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH
export H2_DDS_IFACE=eth10
```

Deploy dal PC:

```powershell
python scripts/deploy_h2_demo_to_jetson.py
```

### 3.4 Audio

| File | Origine | Formato richiesto |
|------|---------|-------------------|
| `pellegrino_tts.wav` | `scripts/generate_pellegrino_tts_wav.py` (edge-tts IT) | 16 kHz, mono, s16 |
| `cyberpunk_meme.wav` | conversione da MP3 con ffmpeg | 16 kHz, mono, s16 |

```powershell
ffmpeg -y -i "$env:USERPROFILE\Downloads\cyberpunk-2077.mp3" -ar 16000 -ac 1 -sample_fmt s16 data/audio/cyberpunk_meme.wav
python scripts/generate_pellegrino_tts_wav.py
```

---

## 4. Struttura del repository (PC lab)

### 4.1 Script sulla Thor (deployati)

| Script | Ruolo |
|--------|--------|
| **`h2_common.py`** | DDS, FSM, `rt/arm_sdk`, pose braccia, rampa smooth |
| **`h2_hand_util.py`** | Probe e grip BrainCo left/right via DDS |
| **`h2_wav_util.py`** | Lettura WAV e streaming PCM verso `AudioClient` |
| **`h2_demo_casoria.py`** | **Demo principale** — TTS + meme + thread braccio/mano |
| `h2_left_arm_raise.py` | Solo braccio sinistro (test) |
| `h2_right_arm_hand.py` | Braccio destro + mano (test legacy) |
| `h2_recover_stand.py` | Check FSM / select mode `ai` (no Damp default) |
| `h2_probe_lowstate.py` | Verifica `rt/lowstate` |
| `h2_probe_hand_dds.py` | Verifica `rt/brainco/*/state` |
| `h2_test_tts.py`, `h2_play_wav.py` | Test audio |
| `h2_arm_emergency_stop.py` | Stop emergenza publisher arm |
| Altri `h2_arm_*`, `h2_verify_*` | Diagnostica braccio / SDK |

### 4.2 Script solo sul PC lab (non deployati sulla Thor)

| Script | Ruolo |
|--------|--------|
| **`h2_smoke_remote.py`** | SSH verso Thor, un passo smoke alla volta |
| **`deploy_h2_demo_to_jetson.py`** | SFTP + `install_sdk.sh` |
| **`h2_start_brainco_pc2.py`** | Avvio `brainco_hand_server` su `.162` + verify DDS |
| **`h2_pc2_readonly_check.py`** | Probe read-only PC2 (processo, tty, install dir) |
| `h2_bundle_offline_deps.py` | Tar CycloneDDS per install offline Thor |
| `generate_pellegrino_tts_wav.py` | Genera TTS WAV |

### 4.3 Documentazione (pacchetto mail / repository PC lab)

| File | Contenuto |
|------|-----------|
| **`02_GUIDA_COMPLETA.md`** | Guida tecnica completa |
| `01_QUICK_START.md` | Cheat sheet rapido |
| `03_DOVE_TROVARE_SUL_ROBOT.md` | Percorsi su Thor, PC2, PC lab |
| `README_THOR.md` | README su `~/h2_demo/README.md` sulla Thor |

---

## 5. Come funziona il codice

### 5.1 Inizializzazione DDS — `h2_common.init_dds`

```python
ChannelFactoryInitialize(0, iface)  # iface default: env H2_DDS_IFACE o "eth10"
```

Tutti gli script chiamano `init_dds()` all’avvio. L’interfaccia deve essere quella collegata alla LAN Unitree (`eth10` sulla Thor).

### 5.2 Braccio — pattern `rt/arm_sdk` (xr H2)

Basato su **xr_teleoperate** `H2_ArmController` e esempio SDK `g1_arm7_sdk_dds_example.py`:

1. **Precheck FSM** (`check_h2_arm_motion_ready`):
   - FSM **vietati** per movimento braccio: `0`, `1` (Invalid, Passive/Damp)
   - FSM **ammessi** in motion mode: `2`, `4`, `5`, `601`, `701`, `703`
2. Subscribe `rt/lowstate` (HG message, 35 motori osservati in lab).
3. Publisher `rt/arm_sdk` a **250 Hz** (`control_dt = 0.004`).
4. **Blocca tutti i 31 joint** del corpo con gains xr (`_lock_h2_xr_lowcmd`).
5. Anima solo i joint del braccio target con rampa **smoothstep** (`rise` → `hold` → `lower`).
6. Slot **31** = peso/abilitazione arm SDK (come firmware H2).

Pose demo braccio sinistro (`LEFT_ARM_TARGET_Q` in `h2_common.py`):

| Joint | Nome approssimativo | q target |
|-------|---------------------|----------|
| 15 | shoulder pitch | -0.78 (alto) |
| 16 | roll | 0.30 |
| 17 | yaw | 0.10 |
| 18 | elbow | 0.45 |
| 19–21 | polso roll/pitch/yaw | 0.35 / -0.22 / 0.15 |

API locomozione usate (senza rilasciare mode):

- `7007` — `GetArmSdkStatus`
- `7109` — `SetArmSdkStatus`
- `7001` — lettura FSM id

### 5.3 Mani BrainCo — `h2_hand_util.py`

Topic DDS (message type `MotorCmds_` / `MotorStates_` da idl `unitree_go`):

| Topic | Direzione |
|-------|-----------|
| `rt/brainco/left/cmd` | comandi mano sinistra |
| `rt/brainco/left/state` | stato |
| `rt/brainco/right/cmd` | comandi mano destra |
| `rt/brainco/right/state` | stato |

Sei motori per mano: thumb, thumb-aux, index, middle, ring, pinky.  
`q ∈ [0, 1]`: 0 = aperto, 1 = chiuso.

`run_gentle_left_grip` / `run_gentle_right_grip`: rampa smooth su `cmd`, hold, riapertura.

### 5.4 la demo — `h2_demo_casoria.py`

Flusso logico:

```
main()
  init_dds()
  [step 1] play pellegrino_tts.wav (bloccante)
  [step 2] avvia thread meme audio (daemon)
           avvia thread mano (delay = rise*0.5)
           run_gentle_left_arm() nel thread principale
           wait audio_done
```

Parametri CLI importanti:

| Flag | Default | Significato |
|------|---------|-------------|
| `--rise` | 8.0 s | salita braccio |
| `--hold` | auto da durata meme | pausa in alto |
| `--lower` | 8.0 s | discesa |
| `--hand-close` | 0.72 | frazione chiusura dita |
| `--dry-arm` / `--dry-hand` / `--dry-audio` | off | test parziali |
| `--yes` | off | salta prompt interattivo (usato da smoke) |

Il thread mano chiama `probe_left_hand()` prima del grip; se DDS assente, errore chiaro.

### 5.5 Audio — `AudioClient` (API G1 sul H2)

`h2_demo_casoria` e `h2_wav_util` usano `unitree_sdk2py.g1.audio.g1_audio_client.AudioClient`:

- WAV **16 kHz, mono, PCM s16**
- `PlayStream` + `PlayStop` per ogni file

### 5.6 Smoke remoto — `h2_smoke_remote.py`

Dal PC: SSH + comando remoto con prefisso env CycloneDDS.

Step principali:

| Step | Sicurezza |
|------|-----------|
| `ping`, `lowstate`, `hand-dds`, `tts`, `wav` | sicuri |
| `arm`, `demo`, `demo-slow` | richiedono `--confirm-arm` |
| `recover-select-ai`, `recover-stand` | richiedono `--confirm-recovery` |

### 5.7 Recovery — `h2_recover_stand.py`

- `--check`: legge modalità locomozione e FSM senza muovere nulla.
- `--select-ai`: imposta mode `ai` via `MotionSwitcher` se necessario.
- `--recover`: sequenza stand **senza Damp di default** (pericoloso sul frame).

Preferire `recover-select-ai` se il robot è già in piedi sul protection frame.

---

## 6. Runbook operativo (giorno demo)

### 6.1 Checklist pre-demo

Vedi `00_ACCENSIONE_ACCESSI_E_STATO_ROBOT.md` per il flusso completo. In sintesi:

1. Accensione telecomando: Zero Torque → Damping → Locked Standing → **piedi a terra** → **Regular Mode 1**
2. Cavo Ethernet PC ↔ LAN `192.168.123.x`
3. Robot stabile sul protection frame, area libera braccio sinistro
4. PC2 acceso → `python scripts/h2_start_brainco_pc2.py`
5. `recover-check` — FSM non 0/1; `hand-dds` — OK
6. Operatore con telecomando

### 6.2 Comandi (ordine consigliato)

```powershell
# 1) Bridge mani sul PC2 (dopo reboot PC2)
python scripts/h2_start_brainco_pc2.py

# 2) Verifiche
python scripts/h2_smoke_remote.py recover-check
python scripts/h2_smoke_remote.py hand-dds
python scripts/h2_smoke_remote.py lowstate

# 3) Demo completa
python scripts/h2_smoke_remote.py demo --confirm-arm
```

### 6.3 Esecuzione diretta sulla Thor (senza smoke)

```bash
ssh unitree@192.168.123.163
cd ~/h2_demo
export CYCLONEDDS_HOME=/usr/local LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH H2_DDS_IFACE=eth10
python3 scripts/h2_probe_hand_dds.py --iface eth10
python3 scripts/h2_demo_casoria.py --iface eth10 --yes
```

### 6.4 Dopo modifiche al codice

```powershell
python scripts/deploy_h2_demo_to_jetson.py
python scripts/h2_smoke_remote.py hand-dds
```

---

## 7. Troubleshooting

| Sintomo | Causa probabile | Azione |
|---------|-----------------|--------|
| `hand-dds` vuoto dalla Thor | `brainco_hand_server` spento su PC2 | `python scripts/h2_start_brainco_pc2.py` |
| SSH PC2 rifiutato | Password errata | Usare **`Unitree#24226`** |
| `rt/lowstate` timeout | Cavo LAN / interfaccia sbagliata | Verificare `eth10`, ping `.161`/robot |
| Arm rifiutato (FSM 1) | Robot in Damp/Passive | Ripristinare HybridWalk/Protection da telecomando |
| Audio assente | WAV formato errato | Riconvertire a 16 kHz mono s16 |
| Thor non raggiungibile | IP o rete lab | Verificare `192.168.123.163` |

Probe read-only PC2:

```powershell
python scripts/h2_pc2_readonly_check.py
```

Log server mani su PC2: `/tmp/brainco_hand_server.log`

---

## 8. Sicurezza — riepilogo

1. **Mai** `ReleaseMode()` durante il demo.
2. **Mai** Damp automatico con protection frame.
3. Braccio solo se FSM ∉ `{0, 1}`.
4. `--confirm-arm` obbligatorio per step arm/demo da `h2_smoke_remote.py`.
5. Operatore con telecomando, area sgombra.
6. Mani: solo DDS; PC2 gestisce la seriale.

---

## 9. Riferimenti esterni

- [Unitree H2 ROS2 / rete](https://support.unitree.com/home/en/H2_developer/ros2_communication_routine)
- [BrainCo Hand (doc G1, stesso stack)](https://support.unitree.com/home/en/G1_developer/brainco_hand)
- [brainco_hand_service (GitHub)](https://github.com/unitreerobotics/brainco_hand_service)
- [unitree_ros2 — esempi H2](https://github.com/unitreerobotics/unitree_ros2)
- SDK Python: `unitree_sdk2_python` — `example/g1/high_level/g1_arm7_sdk_dds_example.py`

---

## 10. Stato verificato in laboratorio (luglio 2026)

- Thor `~/h2_demo`: SDK + CycloneDDS OK, audio presenti, 21 script deployati
- `lowstate` OK (35 motori), `hand-dds` OK con server PC2 attivo
- la demo completa ~30 s, exit 0 via `h2_smoke_remote.py demo --confirm-arm`
- PC2: `brainco_hand_server` avviato via script PC, **nessuna modifica strutturale** alla macchina `.162`

---

*Ultimo aggiornamento: allineato al repository `mujoco_go2_d1` — Demo H2 su Jetson Thor.*

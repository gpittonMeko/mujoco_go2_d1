# Worker grasp HTTP (OpenVLA / stub) per dashboard operator

La dashboard sulla Jetson fa **proxy** verso questo processo (`GET /health`, `POST /plan`, `POST /execute`). Variabile sulla NX: `GO2_ANYGRASP_WORKER_URL` (nome storico; il backend può essere OpenVLA, stub, GraspNet, ecc.).

## Separazione dal monolite

- **Monolite** = processo Flask `serve_dashboard_modular` + modulo `diagnostics_dashboard` (es. porta 5050). **Questo worker non lo importa e non lo avvia.**
- **Dashboard operator** = `serve_dashboard_lite` + `go2_dashboard/lite_app.py` (porta 5052): è ciò che fa proxy HTTP verso questo worker.
- Il backend **`planner`** (default) carica **solo** moduli Python in `scripts/box_grasp_planner.py` (e `arm_kinematics_d1_template.py`): sono librerie di geometria/presa, **non** il server monolite. Servono sul PC RTX perché i file stanno nel repo; non c’è chiamata HTTP al monolite.
- `POST /execute` qui **non** muove il braccio: resta responsabilità della catena che già usi sulla NX (operator / DDS), indipendente dal monolite.

## Rete consigliata

- Jetson sulla LAN Unitree, es. `192.168.123.18`.
- PC con RTX 5070 con indirizzo **sulla stessa subnet**, es. `192.168.123.4`, così la NX raggiunge il worker senza route extra.
- Un IP tipo `172.20.192.1` spesso è un’altra interfaccia/VPN: dalla NX **può non essere raggiungibile** finché non c’è routing o un IP `192.168.123.x` sul PC.

## Un solo comando sul PC (stessa rete della Jetson)

### Linux

```bash
bash external/openvla_worker/bootstrap_worker_host.sh
```

### Windows (PowerShell — **un solo comando**, barra `tqdm` durante `pip install`)

Sostituisci il percorso se il repo non è lì:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\user\MujocoCaneD1\mujoco_go2_d1\external\openvla_worker\bootstrap_worker_host.ps1"
```

Lo script crea `external/openvla_worker\.venv`, installa dipendenze con `setup_windows_worker.py` ( **`tqdm`** su ogni riga di `requirements.txt` ), stampa gli IP e avvia Flask su `0.0.0.0:8765`.

Seconda esecuzione Linux (venv già pronto): `bash external/openvla_worker/bootstrap_worker_host.sh --skip-install`

Opzionale — **solo clone git** del repo OpenVLA in `~/source/openvla` (nessun `pip` extra qui):

```bash
bash external/openvla_worker/bootstrap_worker_host.sh --with-openvla
```

Windows:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "...\external\openvla_worker\bootstrap_worker_host.ps1" -WithOpenvla
```

## Avvio rapido (stub, sviluppo)

```bash
cd external/openvla_worker
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export WORKER_BIND_HOST=0.0.0.0
export WORKER_PORT=8765
python app.py
```

Verifica da un altro terminale (dalla **root del repo**):

```bash
python scripts/verify_anygrasp_worker_http.py http://127.0.0.1:8765
```

## Produzione (gunicorn)

```bash
gunicorn -w 1 -b 0.0.0.0:8765 app:app
```

## Docker

Vedi `Dockerfile` (stub senza GPU). Per OpenVLA reale serve immagine base **NVIDIA CUDA** + dipendenze del progetto ufficiale.

## Backend ``openvla`` (worker Flask)

Imposta `GO2_GRASP_WORKER_BACKEND=openvla` sul PC worker.

### Un solo endpoint verso la NX (8765)

- La dashboard operator sulla Jetson parla **solo** con `GO2_ANYGRASP_WORKER_URL` → tipicamente `http://<IP_RTX>:8765` (questo processo Flask).
- Se usi anche un server OpenVLA separato (es. processo su **:8000** con `POST /act`), tienilo su **`127.0.0.1`** (bind localhost) e collegalo al worker con **`OPENVLA_ACT_SERVER_URL`**. **Non** esporre la porta 8000 sulla LAN: firewall / bind solo loopback in produzione.

### Modalità (ordine di priorità in `openvla_runtime.plan_from_openvla_json`)

1. **Dev / test UI:** `OPENVLA_RUNTIME_STUB=1` — scarica il JPEG da `image_url` / `WORKER_CAMERA_JPG_URL` e risponde con una presa sintetica in `base_link` (nessun modello VLM).
2. **Adapter esterno:** `OPENVLA_ADAPTER_CALLABLE=mypackage.modulo:nome_funzione` — la funzione riceve `(body, jpeg_bytes)` o keyword `body=`, `jpeg_bytes=` e deve restituire un **dict** con le chiavi attese dalla dashboard (`grasp_display_base_link_m`, …). `OPENVLA_REPO_ROOT` (opzionale) viene anteposto a `sys.path` prima dell’import.
3. **Server HTTP /act (stesso PC, dietro le quinte):** `OPENVLA_ACT_SERVER_URL=http://127.0.0.1:8000` — il worker fa `GET` del JPEG dalla URL polso (come le altre modalità), poi `POST` JSON verso `{OPENVLA_ACT_SERVER_URL}{OPENVLA_ACT_PATH}` (default path `/act`). La risposta viene mappata a `openvla_action_7dof` + campi UI (`grasp_display_base_link_m` via euristica `OPENVLA_HEURISTIC_*`). **Priorità prima di HF:** se `OPENVLA_ACT_SERVER_URL` è impostato, non serve `OPENVLA_USE_HF` sul worker salvo fallback voluto (rimuovi ACT URL se vuoi solo HF).
4. **Hugging Face (modello pretrain ufficiale):** `OPENVLA_USE_HF=1` — carica `openvla/openvla-7b` (override con `OPENVLA_HF_MODEL_ID` o path locale in `OPENVLA_CHECKPOINT`) con `transformers` + `predict_action` come nel README openvla/openvla. Serve **GPU CUDA** e RAM VRAM adeguata (~14GB+ per 7B in fp16/bf16).
5. **Solo codice training:** clone separato (`bash … --with-openvla` o PowerShell `-WithOpenvla`) in `~/source/openvla` — utile per sviluppo; l’inferenza HF non richiede quel clone se usi il modello da Hub.

#### Variabili `OPENVLA_ACT_*` (server /act separato)

| Variabile | Default | Ruolo |
|-----------|---------|--------|
| `OPENVLA_ACT_SERVER_URL` | — | Base URL, es. `http://127.0.0.1:8000` (senza slash finale). |
| `OPENVLA_ACT_PATH` | `/act` | Path sul server ACT. |
| `OPENVLA_ACT_USE_IMAGE_URL` | `0` | Se `1`, il payload invia l’URL immagine invece del JPEG base64 (vedi chiavi sotto). |
| `OPENVLA_ACT_JSON_IMAGE_KEY` | `image` | Nome campo per immagine base64 (stringa ASCII). |
| `OPENVLA_ACT_JSON_INSTRUCTION_KEY` | `instruction` | Nome campo per il testo istruzione. |
| `OPENVLA_ACT_JSON_IMAGE_URL_KEY` | `image_url` | Se `OPENVLA_ACT_USE_IMAGE_URL=1`, campo URL (il worker usa `body.image_url` / `camera_jpg_url` se presenti). |
| `OPENVLA_ACT_TIMEOUT_S` | `120` | Timeout `POST` verso il server ACT. |
| `OPENVLA_ACT_FETCH_TIMEOUT_S` | `25` | Timeout download JPEG dalla camera NX. |

Il corpo `POST /plan` può includere `act_server_extra` (oggetto JSON): le coppie chiave/valore vengono fuse nel payload verso `/act` senza sovrascrivere i campi già impostati dal worker.

### Attivazione rapida OpenVLA HF (Windows RTX)

1. Installa **PyTorch con CUDA** nel venv del worker (da pytorch.org, versione compatibile con la tua GPU).
2. Dipendenze HF (una volta):

   ```powershell
   cd …\mujoco_go2_d1\external\openvla_worker
   .\.venv\Scripts\python.exe -m pip install -r requirements-openvla.txt
   ```

   Oppure da zero: `powershell -ExecutionPolicy Bypass -File .\bootstrap_worker_host.ps1 -InstallOpenvlaHF` (include anche `requirements.txt`).

3. Avvio worker:

   ```powershell
   $env:GO2_GRASP_WORKER_BACKEND = "openvla"
   $env:OPENVLA_USE_HF = "1"
   $env:OPENVLA_UNNORM_KEY = "bridge_orig"
   $env:OPENVLA_DEFAULT_INSTRUCTION = "pick up the object"
   $env:WORKER_CAMERA_JPG_URL = "http://192.168.123.18:5052/api/robot/camera/0.jpg"
   .\.venv\Scripts\python.exe app.py
   ```

   Il primo `POST /plan` scarica i pesi da Hugging Face (tempo lungo) e può richiedere molti GB su disco.

- **`OPENVLA_UNNORM_KEY`:** per checkpoint pretrain Hub usare in genere `bridge_orig` (come negli esempi ufficiale); per modelli fine-tunati usare la chiave del dataset usato in training e, se presente, `dataset_statistics.json` nella cartella del checkpoint.
- **Visualizzazione 3D / `base_link`:** con l’azione **bridge** standard, `grasp_display_base_link_m` è ancora **euristica** (`OPENVLA_HEURISTIC_ORIGIN_M`, `OPENVLA_HEURISTIC_ACTION_SCALE`) sui primi 3 numeri — non è la cinematica D1. Per allineare il marker alla **stessa FK** usata da `scene_3d` sul braccio reale, imposta **`OPENVLA_ACTION_FK_JOINTS=1`**: i primi 6 valori dell’azione sono interpretati come **q in radianti** (assoluti) per `scripts/arm_kinematics_d1_template.py`; il worker aggiunge `openvla_fk_tool_tip_base_link_m`, `openvla_joint_space=d1_rad` e sovrascrive `grasp_display_base_link_m` con quella punta. In alternativa, sulla **Jetson** (dashboard lite) puoi usare `GO2_SCENE3D_OPENVLA_FK_MODE=absolute|delta` + `GO2_SCENE3D_OPENVLA_DELTA_SCALE` per ricalcolare la punta dall’ultimo piano in cache senza toccare il worker (solo se i 6 numeri sono davvero giunti). Con `openvla_joint_space=d1_rad`, sulla NX (dopo `GO2_ENABLE_REAL_ARM=1`) abilita **`GO2_ENABLE_ARM_PLAN_EXECUTE=1`** (deploy default) o **`GO2_ENABLE_OPENVLA_ARM_EXECUTE=1`** e usa **Muovi braccio D1 (ultimo piano FK)** → `POST /api/arm/openvla_execute_last_plan_d1` con `{"confirm":"MOVE_D1_OPENVLA"}`. **Senza** giunti D1 nel JSON ma con un punto 3D (`grasp_display_base_link_m`, `openvla_fk_tool_tip_base_link_m`, ecc.): tab Robot → **Muovi braccio (IK ultimo piano)** → `POST /api/arm/execute_last_plan_ik` con `{"confirm":"MOVE_IK_CACHED"}` (stesso flag env; opzionale `GO2_GRASP_IK_OFFSET_Z_BASE_LINK_M`).

Verifica GPU / import (dalla root `mujoco_go2_d1`):

```bash
python scripts/verify_openvla_rtx_env.py
```

## Collegare OpenVLA vero (integrazione laboratorio)

1. Repository ufficiale: [openvla/openvla](https://github.com/openvla/openvla) (versioni CUDA/Python compatibili con la GPU, es. RTX 5070).
2. Integrazione consigliata sulla RTX: **un solo worker :8765** verso la NX; policy su **:8000** solo **localhost** e `OPENVLA_ACT_SERVER_URL=http://127.0.0.1:8000` sul worker. In alternativa `OPENVLA_USE_HF=1` direttamente nel worker, oppure **adapter** (`OPENVLA_ADAPTER_CALLABLE`) per output custom.
3. Avviare il worker sulla porta scelta; sulla NX impostare `GO2_ANYGRASP_WORKER_URL=http://<IP_PC>:8765` (come per il backend planner).

### Checklist rapida RTX (laboratorio)

- Fermare vecchi processi sulla **8765** (e, se usi ACT interno, avviare il server su **127.0.0.1:8000**).
- `git pull` sul branch usato in lab; venv worker aggiornato.
- `GO2_GRASP_WORKER_BACKEND=openvla` + env per **ACT** (`OPENVLA_ACT_SERVER_URL`) o **HF** (`OPENVLA_USE_HF=1` + torch CUDA + `requirements-openvla.txt`) o **stub** per test.
- `curl -s http://127.0.0.1:8765/health` → `implementation` del repo e `openvla_status.plan_mode_priority_it`.
- `python scripts/verify_anygrasp_worker_http.py` (opzionale strict: `GO2_VERIFY_WORKER_PLAN_KEYS=1`).

## Deploy env sulla Jetson

Lo script `scripts/deploy_dashboard_to_nx.py` scrive `GO2_ANYGRASP_WORKER_URL` in `nx_dashboard_env.sh`.

- Default deploy: `http://192.168.123.4:8765`
- Override da PC al deploy:  
  `set GO2_DEPLOY_ANYGRASP_WORKER_URL=http://192.168.123.4:8765` (PowerShell)  
  poi `python scripts/deploy_dashboard_to_nx.py`

## Probe reachability dalla NX

Sul PC (repo root):

```bash
python scripts/probe_grasp_worker_network_on_nx.py 192.168.123.4 8765
```

Esegue ping verso `192.168.123.4` e `172.20.192.1` (override con `GO2_PROBE_ALT_WORKER_IP`) e `curl /health` dalla Jetson.

# Dashboard operator (NX)

Questo documento riguarda **solo** il processo **lite** su `GO2_DASHBOARD_PORT` (default **5052**): `scripts/serve_dashboard_lite.py` → `go2_dashboard.lite_app.create_operators_app()`. **Non** descrive la dashboard monolite (`diagnostics_dashboard` / `serve_dashboard_modular`, altra porta es. 5051), che monta migliaia di route legacy.

## Stato: cosa funziona e cosa no (porta 5052)

| Area | Funziona | Limitazioni / non disponibile |
|------|----------|-------------------------------|
| **HTTP + UI** | `GET /` → `dashboard_operators.html` (titolo **Go2 operator**). `GET /api/health`, `GET /api/status`. | — |
| **Meta** | `GET /api/modular/info`. | Non implica che il monolite sia caricato: in questo processo **non** lo è. |
| **Camere** | `GET /api/cameras/status`, JPEG/MJPEG log.0 e log.6, `GET /api/robot/vla_frame.jpg` (con env), debug V4L se abilitato, `GET /api/vision/box_detect` (NX + `GO2_LOCAL` + OpenCV/NumPy). | Fuori NX / senza `GO2_LOCAL` parte della diagnostica è ridotta o assente. |
| **Base / Sport** | `GET /api/base/sport_last`, `GET/POST /api/base/accompany_mode`. | Richiede stack DDS / env coerenti (`nx_dashboard_env.sh`). |
| **Stack NX** | `GET /api/nx/stack/status`, `POST /api/nx/stack/start` (avvio cache cam 0/6). | Copre solo `operator_stack`, non tutto il monolite. |
| **Braccio D1** | Home, true zero, START da JSON, sequenze, IK su ultimo piano in cache, giunti OpenVLA, e-stop HTTP, `scene_3d` / mesh. | **Nessuna** route `/api/box/*`. **POST** `/api/alignment/start_pose` → **501** (salvataggio START con planner AprilTag = monolite). |
| **Grasp / VLA** | `GET/POST /api/grasp/*` → proxy a `GO2_ANYGRASP_WORKER_URL`; cache in `operator_plan_cache`. | Worker **esterno** obbligatorio per piani reali; senza di esso health/plan falliscono. |
| **Hermes (LLM)** | `GET /api/hermes/status`, `POST /api/hermes/command` se agente e API key attivi. | Default deploy: `GO2_ENABLE_HERMES_AGENT=0` (spento). |
| **Calibrazione tag5** | `tag5_calibration_lite` (flow, tag5, dual condiviso). | Altro dal pannello calibrazione «pieno» resta sul processo legacy se serve. |
| **Mission** | `GET /api/mission/console`, `POST /api/mission/dashboard_restart` (con `GO2_MISSION_ADMIN_TOKEN`). | Il riavvio colpisce solo `serve_dashboard_lite.py` (supervisore lo rilancia). |
| **Geometria 3D** | `data/vis_geometry_tuning.json`; preset in `vis_geometry_presets.json` (deploy non sovrascrive il file remoto se già esiste). | **No** `GET /api/arm/vis_geometry` come sul monolite; tuning via file o estensione futura. |

**Verifica da PC** (home riconosciuta anche con titolo **Go2 operator**):

```bash
python scripts/verify_dashboard_http.py http://192.168.123.18:5052
```

---

HTTP **operator** sulla Jetson: **non importa** `diagnostics_dashboard` né monta le sue route: API nel package `go2_dashboard/blueprints/operator_api/` (`routes.py`, `helpers_*.py`) + proxy grasp in `go2_dashboard/blueprints/grasp.py` verso un worker esterno.

Il **worker grasp** sul PC (es. `external/openvla_worker`) **non** deve importare il monolite: usa moduli in `scripts/` (piano geometrico) se `GO2_GRASP_WORKER_BACKEND=planner`, oppure `openvla_runtime` se `openvla`; vedi `external/openvla_worker/README.md`.

## URL

- In laboratorio (LAN Unitree): `http://192.168.123.18:5052/` (sostituire IP se diverso).
- Health: `GET /api/health` (campi compatibili con `verify_dashboard_http.py`: `ok`, `service: go2_dashboard`).

## Orbbec Gemini (RGB sulla dashboard)

- **Documentazione:** [Orbbec Gemini 335 / 335L](https://www.orbbec.com/docs/orbbec-gemini-335-335l-documentation/) (driver, UVC, profili).
- **Tab Scene** della dashboard operator: URL completi (MJPEG / JPEG singolo frame) e anteprima **Orbbec log. 0** e **RealSense log. 6**.
- **Endpoint HTTP** (stesso host/porta della dashboard, eventuale `script_root` se usi prefisso URL):
  - RGB live Orbbec: `GET …/stream/robot/camera/0.mjpg`
  - Frame JPEG: `GET …/api/robot/camera/0.jpg`
- **Diagnostica:** `GET /api/cameras/status` — mappa V4L/USB (`v4l_usb_auto_map`, `v4l_index_by_logical`, errori cache).
- **NX (env):** il deploy imposta `GO2_ORBBEC_PREFER_MJPEG=1` per preferire MJPEG sull’UVC Orbbec quando supportato (meno carico rispetto a YUYV grezzo).

## Stazione di controllo (tab **Controllo**)

- **UI:** nella dashboard operator, tab *Controllo* — aggrega `GET /api/mission/console` (stack NX, proxy grasp verso il PC worker, variabili env operative non segrete).
- **Script lab:** `python scripts/lab/lab_mission_status.py http://192.168.123.18:5052` — exit code 0 se `ok` e (con proxy grasp attivo) il worker risponde.
- **Riavvio soft Flask sulla NX:** `POST /api/mission/dashboard_restart` con header `X-Mission-Token` uguale a `GO2_MISSION_ADMIN_TOKEN` se questa variabile è definita nel processo (altrimenti usare i comandi SSH mostrati nella UI). Il supervisore `nx_dashboard_supervise.sh` rilancia `serve_dashboard_lite.py`.

## Deploy

Dalla root del repo (PC con SSH verso la NX):

```bash
python scripts/deploy_dashboard_to_nx.py
```

Dopo il deploy, `scripts/nx_dashboard_env.sh` sulla NX viene rigenerato con `GO2_DASHBOARD_PORT=5052` e `GO2_LITE_SKIP_LIDAR=1` (default).

## Checklist VLA / worker OpenVLA (RTX + NX)

Obiettivo: **un solo HTTP worker** esposto alla rete (`:8765` sul PC RTX); eventuale server `POST /act` su **:8000** resta **localhost** sullo stesso PC e viene richiamato dal worker tramite `OPENVLA_ACT_SERVER_URL`. Dettagli env e priorità stub → adapter → ACT → HF: `external/openvla_worker/README.md`.

### PC RTX (inferenza)

1. Repo aggiornato; venv del worker (`external/openvla_worker/.venv` o bootstrap script).
2. `GO2_GRASP_WORKER_BACKEND=openvla` e una tra: `OPENVLA_RUNTIME_STUB=1` (test), `OPENVLA_ACT_SERVER_URL=http://127.0.0.1:8000` (server /act locale), `OPENVLA_USE_HF=1` (torch **CUDA** + `pip install -r external/openvla_worker/requirements-openvla.txt`).
3. `WORKER_CAMERA_JPG_URL` o corpo piano con `image_url` verso JPEG sulla NX (es. `http://192.168.123.18:5052/api/robot/camera/0.jpg`).
4. Verifica: `curl -s http://127.0.0.1:8765/health | jq` — controllare `implementation` / campi `openvla` coerenti con il repo; `POST /plan` con corpo minimo o script `python scripts/verify_anygrasp_worker_http.py` (strict: `GO2_VERIFY_WORKER_PLAN_KEYS=1`).
5. Ambiente GPU (opzionale): `python scripts/verify_openvla_rtx_env.py` dalla root del repo.

### Jetson NX (dashboard lite)

1. `GO2_ANYGRASP_WORKER_URL=http://<IP_RTX_LAB>:8765` (stesso file env generato dal deploy o export manuale prima di `serve_dashboard_lite`).
2. Deploy da PC in laboratorio: `python scripts/deploy_dashboard_to_nx.py` (aggiorna script e doc copiati in `REMOTE_PUSH_FILES`).
3. Tab **Robot** / **3D**: la UI usa campi come `grasp_display_base_link_m`, `openvla_action_7dof` se restituiti da `POST /plan` sul worker.

## Processi

1. **Flask operator** (unico HTTP pubblico su 5052): `nx_dashboard_supervise.sh` → `serve_dashboard_lite.py` → `go2_dashboard.lite_app.create_operators_app()` (route proprie + proxy AnyGrasp).
2. **Worker AnyGrasp** (opzionale): `GET/POST /api/grasp/*` verso `GO2_ANYGRASP_WORKER_URL`.

### Cosa è implementato in operator (senza monolite)

- `GET /api/health`, `GET /api/cameras/status`, stream/JPEG camere, `GET /api/nx/stack/status`, `POST /api/nx/stack/start` (solo camere 0/6), Sport `GET /api/base/sport_last`, `GET/POST /api/base/accompany_mode`, `GET /api/alignment/start_pose` (solo lettura file), `GET /api/arm/scene_3d` (payload ridotto: FK + `data/vis_geometry_tuning.json`), `GET /api/arm/grasp_pipeline` (checklist lite vs monolite).
- **Calibrazione tag5 su lite:** `GET/POST/DELETE /api/arm/tag5_calibration`, `GET /api/arm/calibration_flow`, `GET/POST /api/arm/tag_calibration_shared_dual` (implementazione `go2_dashboard/tag5_calibration_lite.py`, niente monolite).
- `GET /api/mission/console` (stazione di controllo), `POST /api/mission/dashboard_restart` (opzionale, token), `GET /api/grasp/*` (proxy worker).

### Tab 3D (Three.js)

- I dati arrivano dall’implementazione **operator** di `scene_3d` (vedi `go2_dashboard/operator_scene.py`). Non c’è piano AprilTag né mesh STL server-side da questa app.

Variabili utili:

- `GO2_ANYGRASP_PROXY` — `1` (default) tenta il proxy; `0` solo stub lato Flask.
- `GO2_ANYGRASP_CHECKPOINT` — path checkpoint (documentazione SDK AnyGrasp).

## Cosa non è in questa app

- Nessuna route **`/api/box/*`**. **`POST /api/alignment/start_pose`** non è supportato (501): richiede il planner del monolite.
- **`/api/arm/vis_geometry`** (slider persistiti come nel monolite) non è duplicata su lite: la geometria viewer si regola con `data/vis_geometry_tuning.json` / preset; per API slider come la monolite serve ancora il processo 5050 **oppure** estensione futura nel package `go2_dashboard/blueprints/operator_api/`.

## Calibrazione

Tab Calib: link esterni / note; per API calibrazione complete usare la dashboard monolite sulla porta configurata (es. 5050).

## Troubleshooting presa (tab Robot e 3D)

### Tab Robot

- **`GET /api/arm/grasp_pipeline`**: pulsante «Pipeline presa» — su questa dashboard è uno **stub** (`operator_slim`) che spiega che la pipeline end-to-end non è duplicata; per fusion/IK completo usare il monolite o estendere `go2_dashboard/operator_scene.py` / worker.
- **AnyGrasp**: corpo JSON editabile per `POST /api/grasp/plan` e `POST /api/grasp/execute`; checkbox per **unire** l’ultima risposta «Piano» al corpo execute.
- **Anteprima polso (RGB)**: `GET /api/robot/camera/0.jpg` con overlay: `grip_point.cx` / `cy` (pixel), `grip_point.u` / `v` (0–1), oppure array **`operators_overlay_points`** nel JSON (`{ "x", "y", "label" }`) per punti manuali di test.
- **Allineamento con il viewer 3D:** il worker può mandare **`grasp_display_base_link_m`** (o `approach_point_base_link_m`, `target_base_link_m`, `grasp_center_base_link_m`) in **metri**, frame **`base_link`**. Il client disegna subito una **sfera bianca**. La dashboard lite **cache** ogni piano `ok`: in **`GET /api/arm/scene_3d`** compaiono **`worker_plan_grasp_base_link_m`** (sfera **arancione** nel viewer) e `operator_vla_display.distance_tip_to_marker_m` rispetto alla punta FK reale — stesso sistema di coordinate del braccio cilindrico. Per azioni VLA = **giunti** in radianti: `OPENVLA_ACTION_FK_JOINTS=1` sul worker, oppure `GO2_SCENE3D_OPENVLA_FK_MODE=absolute|delta` sulla NX; altrimenti OpenVLA «bridge» resta euristica sui primi 3 numeri (vedi `external/openvla_worker/README.md`).

### Tab 3D

- Viewer **Three.js** (CDN): poll su **`/api/arm/scene_3d`** — payload costruito in **`go2_dashboard/operator_scene.py`** (nessun `diagnostics_dashboard`).

## Policy dimensione codice

- **Nuove API operator (porta 5052):** aggiungere in `go2_dashboard/blueprints/operator_api/` o moduli dedicati sotto `go2_dashboard/` — **non** in `diagnostics_dashboard.py`.
- **Legacy monolite (`diagnostics_dashboard` / porta 5050):** usato raramente — **congelato**: solo fix critici, niente nuove feature.
- **Script lab one-off:** prefisso `_` o cartella `scripts/lab/`; non inserire in `REMOTE_PUSH_FILES` di `deploy_dashboard_to_nx.py` senza review esplicita.
- **Soglia soft:** evitare nuovi file Python monolitici oltre ~400 righe senza split per dominio (arm, hermes, camere, ecc.).

## Preset geometria

`data/vis_geometry_presets.json` sulla NX non viene sovrascritto dal deploy se esiste già (comportamento invariato dello script di deploy).

## AnyGrasp: come si installa davvero (non è nella dashboard)

Questa repository **non contiene** l’SDK AnyGrasp né i pesi: la dashboard fa solo **proxy HTTP** verso un processo separato (`GO2_ANYGRASP_WORKER_URL`, default `http://127.0.0.1:8765`). Di seguito l’ordine operativo reale.

### 1. Repository e documentazione ufficiale

- SDK: [graspnet/anygrasp_sdk](https://github.com/graspnet/anygrasp_sdk) (README principale + cartella `grasp_detection/`).
- **Licenza obbligatoria:** [license_registration/README.md](https://github.com/graspnet/anygrasp_sdk/blob/main/license_registration/README.md) — sulla macchina dove girerà l’inference esegui `./license_checker -f`, invii il **feature id** (senza `%` finale) nel [modulo](https://forms.gle/XVV3Eip8njTYJEBo6), ricevi uno **zip** con `license/` (`licenseCfg.json`, `.lic`, chiavi, ecc.).
- Copi la cartella `license` sotto `grasp_detection/` (e dove richiesto da `grasp_tracking/`), come da loro guida.

### 2. Dipendenze (macchina inference)

Dal README AnyGrasp: **Python**, **PyTorch con CUDA**, **MinkowskiEngine** (spesso build lunga), modulo **pointnet2**, pesi nel percorso che indicano loro (es. sotto `log/` in `grasp_detection`). Seguire le versioni indicate nel README del tag che usate.

### 3. Binari `gsnet.so` / Jetson vs PC (**importante**)

Nella cartella `grasp_detection/gsnet_versions/` del repo pubblico compaiono solo librerie del tipo **`cpython-*-x86_64-linux-gnu.so`**: sono per **Linux x86_64**, non per **aarch64** (Jetson).

- **Sulla Jetson (ARM)** non puoi semplicemente copiare quegli `.so` e aspettarti che funzionino: architettura diversa. Se vi servono binari ARM, è materiale da chiedere ai **fornitori del SDK / licenza** (non coperto da questo repo).
- **Percorso pratico in laboratorio:** installare e far girare il **worker AnyGrasp su un PC Linux x86_64 con NVIDIA GPU**, poi sulla NX impostare ad esempio  
  `export GO2_ANYGRASP_WORKER_URL=http://<IP_PC_LAB>:8765`  
  (nello stesso file env che usate per la dashboard, vedi `scripts/nx_dashboard_env.sh` generato dal deploy, o variabile d’ambiente prima di avviare `serve_dashboard_lite.py`). Il PC deve essere raggiungibile dalla NX (stessa LAN; firewall aperto sulla porta del worker).

### 4. Contratto HTTP che si aspetta **questa** dashboard

`go2_dashboard/blueprints/grasp.py` inoltra verso il worker:

| Metodo | Path worker | Uso |
|--------|-------------|-----|
| `GET` | `/health` | diagnostica |
| `POST` | `/plan` | corpo JSON = quello inviato dalla UI |
| `POST` | `/execute` | idem |

L’SDK in sé espone **demo Python** (`demo.py`), non necessariamente questi tre path. Dovete quindi un **processo wrapper** (es. piccolo server Flask/FastAPI) sulla macchina inference che:

1. Implementa `GET /health` (es. `{"ok": true}` + info opzionali).
2. In `POST /plan` e `POST /execute` traduce il JSON della dashboard nelle chiamate al codice AnyGrasp (RGB/depth/intrinseche/point cloud, ecc. secondo il vostro protocollo con il robot).

Senza questo adattatore, anche con SDK installato **la dashboard continua a non “vedere” nulla** sulla porta 8765.

### 5. Verifica rapida

Da una macchina che raggiunge il worker:

```bash
python scripts/verify_anygrasp_worker_http.py http://IP:8765
```

Se `GET /health` fallisce con *Connection refused*, il processo non è in ascolto o l’URL è sbagliato.

### 6. Riepilogo una riga

**Licenza + SDK su macchina x86_64+GPU + wrapper HTTP `/health` `/plan` `/execute` + `GO2_ANYGRASP_WORKER_URL` puntato dalla NX** = catena completa verso la tab Robot di questa dashboard.

## Alternative ad AnyGrasp (stesso slot: worker HTTP)

La dashboard **non è legata al marchio AnyGrasp**: è legata a un **servizio HTTP** con `GET /health`, `POST /plan`, `POST /execute`. Qualsiasi algoritmo può sostituire AnyGrasp se implementate lo **stesso contratto** (e opzionalmente i campi JSON che la UI già visualizza: `grasp_display_base_link_m`, `operators_grasp_points_base_link_m`, overlay 2D, ecc.).

| Obiettivo | Direzione tipica | Note rapide |
|-----------|-------------------|--------------|
| **Sostituire AnyGrasp senza licenza**, grasp 6D da nuvola / RGB-D, ricerca | **GraspNet baseline** ([graspnet-baseline](https://github.com/graspnet/graspnet-baseline)), **Contact-GraspNet**, reti simili su point cloud | Open-source; dovete addestrare o usare pesi pubblicati; inference su **PC x86_64+GPU** o modello leggero se riuscite a portarlo su Jetson. |
| **Più semplice / classico** | **GPD** (Generic Picking), campionamento grasp su nuvola (Open3D) + filtri collisione | Meno “SOTA clutter” ma integrabile in ROS/MoveIt; wrapper HTTP uguale. |
| **Trasparenti / clutter difficile** | Modelli **VLA** (es. linee GraspVLA / sistemi language-guided su arXiv) | Richiedono dataset, GPU, spesso **istruzioni in linguaggio naturale**; non sono drop-in di una riga: cambia il flusso dati e il peso computazionale. |
| **Prototipo subito** | **Euristiche**: piano sopra oggetto segmentato, approach fisso, presa top-down | Nessun training; utile per sbloccare braccio + UI finché non avete un modello. |

**Scelta consigliata in laboratorio (Go2 + D1):** tenere **Flask proxy + tab Robot** com’è; sviluppare un **secondo worker** (nome/porta a piacere) che espone `/health` `/plan` `/execute` e punta `GO2_ANYGRASP_WORKER_URL` a quell’URL — anche se internamente non è più AnyGrasp. Così evitate licenza chiusa e binari **x86_64-only** dell’SDK ufficiale, mantenendo la stessa dashboard.

## Worker su PC RTX: IP e reachability dalla NX

| Indirizzo | Ruolo tipico | Raggiungibilità dalla Jetson (`192.168.123.18`) |
|-----------|--------------|--------------------------------------------------|
| `192.168.123.4` | PC worker sulla **stessa LAN** del cane | OK se il PC ha quell’IP sull’interfaccia verso `192.168.123.0/24` |
| `172.20.192.1` | Spesso altra LAN / VPN / NIC secondaria | **Non garantita** senza routing statico o gateway comune |

- Worker di riferimento nel repo: [external/openvla_worker/README.md](../external/openvla_worker/README.md) (stub HTTP + istruzioni Docker/OpenVLA).
- Deploy: `GO2_ANYGRASP_WORKER_URL` viene scritto in `nx_dashboard_env.sh`; override da PC con `GO2_DEPLOY_ANYGRASP_WORKER_URL` prima di `python scripts/deploy_dashboard_to_nx.py`. **Default nello script di deploy:** `http://192.168.123.3:8765` — in laboratorio spesso il PC worker ha `.3` o `.4` sulla LAN Unitree; verificare con `python scripts/nx_scan_grasp_worker_port.py` / `probe_grasp_worker_network_on_nx.py`.
- Probe da PC: `python scripts/probe_grasp_worker_network_on_nx.py [IP_worker] [porta]`.

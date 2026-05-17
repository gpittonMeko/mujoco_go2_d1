# Dashboard operator (NX)

HTTP **operator** sulla Jetson: processo `scripts/serve_dashboard_lite.py`, porta default **5052** (`GO2_DASHBOARD_PORT`). **Non importa** `diagnostics_dashboard` né monta le sue route: API minime in `go2_dashboard/blueprints/operator_api.py` + proxy grasp HTTP in `grasp.py` verso un worker esterno (nessun monolite sullo stesso processo).

Il **worker grasp** sul PC (es. `external/openvla_worker`) **non** deve importare il monolite: usa solo moduli in `scripts/` (piano geometrico) se in modalità `planner`; vedi `external/openvla_worker/README.md` sezione «Separazione dal monolite».

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

## Verifica da PC

```bash
python scripts/verify_dashboard_http.py http://192.168.123.18:5052
```

## Deploy

Dalla root del repo (PC con SSH verso la NX):

```bash
python scripts/deploy_dashboard_to_nx.py
```

Dopo il deploy, `scripts/nx_dashboard_env.sh` sulla NX viene rigenerato con `GO2_DASHBOARD_PORT=5052` e `GO2_LITE_SKIP_LIDAR=1` (default).

## Processi

1. **Flask operator** (unico HTTP pubblico su 5052): `nx_dashboard_supervise.sh` → `serve_dashboard_lite.py` → `go2_dashboard.lite_app.create_operators_app()` (route proprie + proxy AnyGrasp).
2. **Worker AnyGrasp** (opzionale): `GET/POST /api/grasp/*` verso `GO2_ANYGRASP_WORKER_URL`.

### Cosa è implementato in operator (senza monolite)

- `GET /api/health`, `GET /api/cameras/status`, stream/JPEG camere, `GET /api/nx/stack/status`, `POST /api/nx/stack/start` (solo camere 0/6), Sport `GET /api/base/sport_last`, `GET/POST /api/base/accompany_mode`, `GET /api/alignment/start_pose` (solo lettura file), `GET /api/arm/scene_3d` (payload ridotto: FK + `data/vis_geometry_tuning.json`), `GET /api/arm/grasp_pipeline` (stub narrativo: la pipeline end-to-end resta sul monolite se ti serve).

### Tab 3D (Three.js)

- I dati arrivano dall’implementazione **operator** di `scene_3d` (vedi `go2_dashboard/operator_scene.py`). Non c’è piano AprilTag né mesh STL server-side da questa app.

Variabili utili:

- `GO2_ANYGRASP_PROXY` — `1` (default) tenta il proxy; `0` solo stub lato Flask.
- `GO2_ANYGRASP_CHECKPOINT` — path checkpoint (documentazione SDK AnyGrasp).

## Cosa non è in questa app

- Nessuna route **`/api/box/*`**. **`POST /api/alignment/start_pose`** non è supportato (501): richiede il planner del monolite.
- Le route **`/api/arm/tag5_calibration`**, **`/api/arm/vis_geometry`**, ecc. **non** sono registrate su `serve_dashboard_lite` (restano sulla dashboard monolite).

## Calibrazione

Tab Calib: link esterni / note; per API calibrazione complete usare la dashboard monolite sulla porta configurata (es. 5050).

## Troubleshooting presa (tab Robot e 3D)

### Tab Robot

- **`GET /api/arm/grasp_pipeline`**: pulsante «Pipeline presa» — su questa dashboard è uno **stub** (`operator_slim`) che spiega che la pipeline end-to-end non è duplicata; per fusion/IK completo usare il monolite o estendere `go2_dashboard/operator_scene.py` / worker.
- **AnyGrasp**: corpo JSON editabile per `POST /api/grasp/plan` e `POST /api/grasp/execute`; checkbox per **unire** l’ultima risposta «Piano» al corpo execute.
- **Anteprima polso (RGB)**: `GET /api/robot/camera/0.jpg` con overlay: `grip_point.cx` / `cy` (pixel), `grip_point.u` / `v` (0–1), oppure array **`operators_overlay_points`** nel JSON (`{ "x", "y", "label" }`) per punti manuali di test.
- **Allineamento con il viewer 3D**: se il worker restituisce **`grasp_display_base_link_m`** (o `approach_point_base_link_m`, `target_base_link_m`, `grasp_center_base_link_m`) come `[x,y,z]` in **metri** nel frame `base_link`, il tab **3D** disegna una **sfera bianca** oltre ai marker `scene_3d` (aggiorna con «Avvia aggiornamento»).

### Tab 3D

- Viewer **Three.js** (CDN): poll su **`/api/arm/scene_3d`** — payload costruito in **`go2_dashboard/operator_scene.py`** (nessun `diagnostics_dashboard`).

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
- Deploy: `GO2_ANYGRASP_WORKER_URL` viene scritto in `nx_dashboard_env.sh`; override da PC con `GO2_DEPLOY_ANYGRASP_WORKER_URL` prima di `python scripts/deploy_dashboard_to_nx.py` (default `http://192.168.123.4:8765`).
- Probe da PC: `python scripts/probe_grasp_worker_network_on_nx.py [IP_worker] [porta]`.

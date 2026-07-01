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

## Collegare OpenVLA vero

1. Seguire il repository / istruzioni ufficiale del modello (es. [OpenVLA](https://github.com/openvla/openvla) e derivati; versioni CUDA/Python compatibili con la 5070).
2. In `app.py`, nel branch `OPENVLA_INFERENCE=1`, caricare pesi e mappare:
   - input: JSON del corpo `POST /plan` (immagini base64, URL, path file, istruzioni testuali — **da definire** con il flusso robot);
   - output: campi attesi dalla UI (`grasp_display_base_link_m`, `operators_grasp_points_base_link_m`, `grip_point`, … in **base_link** metri).
3. Avviare il server sulla porta scelta; sulla NX (o al deploy) impostare  
   `GO2_ANYGRASP_WORKER_URL=http://<IP_PC>:8765`.

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

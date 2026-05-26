# Deploy VLA worker Go2/D1 su AWS EC2 (g5.xlarge)

Worker HTTP che riceve **istruzione in linguaggio naturale + JPEG dal cane** (Orbbec polso + RealSense frontale) e risponde con piano presa / azione / gripper.

La **Jetson NX** invia i dati (non il contrario): il cloud non può raggiungere la LAN `192.168.123.x`.

## Contenuto cartella

| File | Ruolo |
|------|--------|
| `Dockerfile` | Immagine CUDA + OpenVLA 7B + gunicorn |
| `docker-compose.yml` | Avvio con GPU + volume cache Hugging Face |
| `.env.example` | Template variabili (copia in `.env`) |
| `entrypoint.sh` | Warmup modello + gunicorn |
| `ec2-setup.sh` | Comandi one-shot su istanza EC2 nuova |
| `nx-env.example` | Variabili da mettere sulla Jetson |

## 1. EC2 — crea istanza

- **AMI:** Ubuntu 22.04 Deep Learning Base GPU (o Amazon Linux 2023 + driver NVIDIA)
- **Tipo:** `g5.xlarge` (1× A10G 24 GB) — minimo per OpenVLA 7B fp16
- **Disco:** 100 GB gp3 (primo download pesi ~15 GB)
- **Security group:** inbound TCP **8765** (o solo da ALB); in produzione metti **ALB :443** davanti

SSH sull’istanza, poi:

```bash
git clone <repo> mujoco_go2_d1
cd mujoco_go2_d1/external/openvla_worker
bash aws/ec2-setup.sh
cp aws/.env.example aws/.env
# modifica aws/.env: GO2_WORKER_TOKEN, HF_TOKEN se serve
docker compose -f aws/docker-compose.yml up -d --build
curl -s http://127.0.0.1:8765/health | python3 -m json.tool
```

### Gate G2 — stub (valida rete senza scaricare 15 GB)

In `aws/.env`:

```
OPENVLA_RUNTIME_STUB=1
OPENVLA_USE_HF=0
```

Riavvia container, poi dal PC:

```bash
python scripts/verify_aws_vla_worker.py http://<IP_EC2>:8765 --token <GO2_WORKER_TOKEN>
```

### Gate G3 — OpenVLA reale

In `aws/.env`:

```
OPENVLA_RUNTIME_STUB=0
OPENVLA_USE_HF=1
```

Primo avvio: 10–30 min (download pesi). Monitora: `docker logs -f <container>`.

## 2. HTTPS (consigliato)

Metti un **Application Load Balancer** con certificato ACM:

- Target group → EC2:8765, health check `GET /health`
- URL finale es. `https://vla-worker.example.com`
- Sulla NX: `GO2_ANYGRASP_WORKER_URL=https://vla-worker.example.com`

## 3. Jetson NX — pescare dati dal cane

Sulla NX (via deploy o `scripts/nx_dashboard_env.sh`):

```bash
export GO2_ANYGRASP_WORKER_URL=https://<ALB-o-IP-EC2>:8765
export GO2_ANYGRASP_PROXY=1
export GO2_GRASP_CLOUD_MODE=1
export GO2_WORKER_TOKEN=<stesso-token-di-aws/.env>
```

Con `GO2_GRASP_CLOUD_MODE=1` la dashboard:

1. Legge **cam 0** (Orbbec polso) e **cam 6** (RealSense) da `CameraCache`
2. Incolla `jpeg_base64` + `jpeg_base64_front` in ogni `POST /api/grasp/plan`
3. Inoltra a AWS con header `X-Worker-Token`

Verifica camere prima:

```bash
curl -s http://192.168.123.18:5052/api/cameras/status | python3 -m json.tool
curl -o /tmp/c0.jpg http://192.168.123.18:5052/api/robot/camera/0.jpg
curl -o /tmp/c6.jpg http://192.168.123.18:5052/api/robot/camera/6.jpg
```

Deploy da PC (rete verso NX):

```powershell
$env:GO2_DEPLOY_ANYGRASP_WORKER_URL="https://vla-worker.example.com"
$env:GO2_DEPLOY_GRASP_CLOUD_MODE="1"
python scripts/deploy_dashboard_to_nx.py
```

Metti `GO2_WORKER_TOKEN` in `scripts/nx_secrets_dashboard.sh` sulla NX (non committare).

## 4. Flusso operatore

1. Tab **Robot** → istruzione IT → **Piano VLA** (`POST /api/grasp/plan`)
2. Marker arancione in viewer 3D
3. **Muovi braccio (IK)** o **Muovi braccio D1 (FK)** → gripper da `gripper_command` nel piano

## 5. Troubleshooting

| Problema | Fix |
|----------|-----|
| `401 unauthorized` | Token NX ≠ token container |
| `image_fetch_failed` su AWS | Attiva `GO2_GRASP_CLOUD_MODE=1` sulla NX (JPEG inline) |
| OOM GPU | Usa g5.xlarge; non g4dn.xlarge per OpenVLA 7B |
| Primo plan lento | Warmup in corso; volume `hf_cache` persiste i pesi |

## 6. Costi indicativi

- g5.xlarge on-demand ~$1/h — spegni quando non in lab
- Volume EBS 100 GB ~$8/mese

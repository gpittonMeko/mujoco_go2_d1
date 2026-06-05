# Pipeline presa 3D — NX + worker AWS

## Architettura

| Componente | Ruolo |
|------------|--------|
| Dashboard NX `:5052` | UI, camere, proxy `/api/grasp/*`, esecuzione braccio D1 |
| EC2 g5 `GO2_ANYGRASP_WORKER_URL` | `POST /plan` con RGB+depth inline, planner o GraspGen |
| GraspGen ZMQ `:5556` (opzionale, stessa EC2) | 6-DOF grasp su point cloud; fallback automatico su planner |

## Variabili NX (deploy)

- `GO2_ANYGRASP_WORKER_URL` — da `data/go2_vla_ec2_state.json` se non imposti `GO2_DEPLOY_ANYGRASP_WORKER_URL`
- `GO2_GRASP_CLOUD_MODE=1` — JPEG inline verso AWS
- `GO2_GRASP_EMBED_RGBD=1` — allega depth V4L al piano
- `GO2_DEPTH_VIDEO_INDEX_0` / `_6` — indice `/dev/videoN` depth (vedi `GET /api/cameras/status`)
- `GO2_DEPTH_SCALE_M_PER_UNIT=0.001` — scala depth grezza → metri
- `GO2_GRASP_COACH_PRIMARY=0` — Grasp Coach solo recovery

## Variabili worker EC2

- `GO2_GRASP_WORKER_BACKEND=auto` — GraspGen ZMQ se up, altrimenti `box_grasp_planner` + detector + depth
- `GO2_GRASP_GEN_ZMQ=tcp://127.0.0.1:5556` — server GraspGen locale
- `GO2_WORKER_TOKEN` — header `X-Worker-Token` (pairing)

## Flusso operatore

1. Tab **Presa** → **Avvia EC2** (se spenta) o **Piano VLA (1 click)**
2. Verifica JSON: `backend` ≠ `stub`, `grasp_assessment.execution_allowed`
3. **Sequenza presa (fasi)** → `POST /api/grasp/execute_phased`
4. Oppure **Muovi IK** per un solo punto
5. Se fallisce → tab coach (recovery hint)
6. Fine sessione → **Stop EC2**

## GraspGen su EC2 (opzionale)

```bash
# Dopo clone GraspGen + modelli:
bash external/openvla_worker/aws/install-graspgen-zmq.sh
docker compose -f external/openvla_worker/aws/docker-compose.yml up -d --build
```

## Verifica

```bash
python scripts/verify_aws_vla_worker.py http://<IP_EC2>:8765
python scripts/verify_dashboard_http.py http://192.168.123.18:5052
```

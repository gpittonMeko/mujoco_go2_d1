# Quickstart VLA AWS ↔ Jetson Go2 — copia/incolla

## 1) AWS EC2 (g5.xlarge, Ubuntu 22.04, 100 GB, SG: TCP 8765 inbound)

SSH sull’istanza:

```bash
# Opzione A — repo già copiato (scp/rsync da PC):
cd ~/mujoco_go2_d1/external/openvla_worker
bash aws/bootstrap-ec2.sh

# Opzione B — clone git:
export GO2_REPO_URL=https://github.com/TUO_ORG/mujoco_go2_d1.git
curl -fsSL -o /tmp/bootstrap-ec2.sh \
  "https://raw.githubusercontent.com/TUO_ORG/mujoco_go2_d1/main/external/openvla_worker/aws/bootstrap-ec2.sh"
# oppure dopo clone:
bash external/openvla_worker/aws/bootstrap-ec2.sh
```

Al termine stampa **`~/go2-vla-pairing.env`** con URL + token.

OpenVLA reale (dopo test stub):

```bash
cd ~/mujoco_go2_d1/external/openvla_worker
export GO2_WORKER_STUB=0
bash aws/bootstrap-ec2.sh
```

---

## 2) PC di lab — collega la Jetson NX

```powershell
cd C:\Users\user\MujocoCaneD1\mujoco_go2_d1

# Copia pairing da EC2 (sostituisci IP):
scp ubuntu@<EC2_IP>:~/go2-vla-pairing.env .

# Configura NX + riavvia dashboard + verifica proxy:
python scripts/pair_nx_aws_vla.py --pairing-file go2-vla-pairing.env --verify

# Test worker diretto dal PC:
python scripts/verify_aws_vla_worker.py http://<EC2_IP>:8765 --token <TOKEN>
```

Alternativa deploy completo:

```powershell
$env:GO2_DEPLOY_ANYGRASP_WORKER_URL="http://<EC2_IP>:8765"
python scripts/deploy_dashboard_to_nx.py
# Poi pair solo per il token:
python scripts/pair_nx_aws_vla.py --pairing-file go2-vla-pairing.env --skip-restart
```

---

## 3) Operatore — dashboard sul cane

Apri **http://192.168.123.18:5052** → tab **Presa**:

1. Scrivi istruzione (*afferra la scatola bianca*)
2. **Piano VLA (1 click)** — invia cam 0+6 ad AWS
3. Tab **3D** — marker arancione
4. **Muovi IK** o **Muovi FK D1**

Tab **Sistema** → mission console → verifica `grasp_worker` reachable.

---

## Token / sicurezza

| Dove | File / env |
|------|------------|
| EC2 container | `external/openvla_worker/aws/.env` → `GO2_WORKER_TOKEN` |
| Jetson NX | `scripts/nx_secrets_dashboard.sh` → `export GO2_WORKER_TOKEN=...` |
| Jetson NX URL | `scripts/nx_dashboard_env.sh` → `GO2_ANYGRASP_WORKER_URL` |

**Non committare** token né `.env` reali.

---

## Troubleshooting rapido

```bash
# Su EC2:
docker logs -f $(docker ps -q | head -1)
curl -s http://127.0.0.1:8765/health

# Sulla NX (SSH):
curl -s http://127.0.0.1:5052/api/grasp/health | python3 -m json.tool
curl -o /tmp/c0.jpg http://127.0.0.1:5052/api/robot/camera/0.jpg
```

| Errore | Fix |
|--------|-----|
| 401 unauthorized | Token NX ≠ EC2 `.env` |
| worker_unreachable | SG EC2 porta 8765; IP pubblico corretto |
| image_fetch_failed | `GO2_GRASP_CLOUD_MODE=1` sulla NX (auto se URL non LAN) |

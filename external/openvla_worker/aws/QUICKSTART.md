# Setup VLA AWS — comando unico

## Un solo comando (PC, repo root)

Prerequisiti:
- AWS CLI configurato (`aws configure` o env `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`)
- Chiave **`C:\Users\user\Documents\LLM_14.pem`** (key pair AWS nome **`LLM_14`**)
- NX accesa su `192.168.123.18`, PC sulla LAN Unitree

Se AWS CLI dà errore SSL su Windows:
```powershell
$env:AWS_CA_BUNDLE = ""
```

### Crea EC2 + Docker + collega NX

```powershell
cd C:\Users\user\MujocoCaneD1\mujoco_go2_d1
powershell -ExecutionPolicy Bypass -File scripts/go2_vla_full_setup.ps1 -InstallPemOnNx -InstallEc2ControlOnNx
```

Fa in sequenza:
1. Crea **g5.xlarge** (o riavvia se esiste già in `data/go2_vla_ec2_state.json`)
2. Security group porte **22** e **8765**
3. Copia worker, avvia Docker OpenVLA (stub prima)
4. Salva `go2-vla-pairing.env` e `data/go2_vla_ec2_state.json`
5. Configura NX (URL worker, token, cloud mode)
6. Opzionale: copia **LLM_14.pem** sulla NX e metadata EC2 per start/stop

### Solo collegare NX (EC2 già su)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/go2_vla_full_setup.ps1 -SkipProvision -InstallPemOnNx
```

---

## Dopo il setup — cosa puoi fare

| Azione | Dove |
|--------|------|
| **Piano presa VLA** (istruzione → cam → AWS → braccio) | Dashboard **Presa** → *Piano VLA (1 click)* |
| **Muovi braccio** IK / FK | Stesso tab → *Muovi IK* / *Muovi FK D1* |
| **Spegni EC2** (risparmio ~$1/h) | PC: `python scripts/aws_vla_ec2_control.py stop` |
| **Accendi EC2** | PC: `python scripts/aws_vla_ec2_control.py start --wait-health` |
| **Stato worker** | `python scripts/aws_vla_ec2_control.py status` |

### Start/stop EC2 dalla NX del cane

Dopo `-InstallEc2ControlOnNx`, sulla NX aggiungi in  
`/home/unitree/go2_visual_dashboard/scripts/nx_secrets_dashboard.sh`:

```bash
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=eu-west-1
```

Poi SSH sulla NX:
```bash
cd ~/go2_visual_dashboard
python3 scripts/aws_vla_ec2_control.py status
python3 scripts/aws_vla_ec2_control.py stop
python3 scripts/aws_vla_ec2_control.py start --wait-health
```

Serve **AWS CLI** sulla NX (`sudo apt install awscli`) oppure usa i comandi dal PC.

Chiave PEM sulla NX: `~/.ssh/LLM_14.pem` (per SSH debug `ssh -i ~/.ssh/LLM_14.pem ubuntu@<IP_EC2>`).

---

## OpenVLA reale (dopo test stub)

SSH su EC2:
```bash
ssh -i ~/Documents/LLM_14.pem ubuntu@<IP_EC2>
cd mujoco_go2_d1/external/openvla_worker
# in aws/.env: OPENVLA_RUNTIME_STUB=0 OPENVLA_USE_HF=1
docker compose -f aws/docker-compose.yml up -d --build
```

Poi dalla NX rifare un piano VLA (token invariato se non ricrei container).

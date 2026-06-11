# AWS CloudShell — comando unico VLA

Regione: **eu-north-1** (Stockholm).

---

## Chiavi SSH — spiegazione semplice

| Cosa | Dove | Serve per CloudShell? |
|------|------|------------------------|
| **Key pair `LLM_14`** (parte pubblica) | AWS → EC2 → Key pairs → **eu-north-1** | **Opzionale** — solo se vuoi SSH dal PC |
| **`LLM_14.pem`** (parte privata) | Sul tuo PC, es. `Documents\LLM_14.pem` | **NO** — non serve caricarla in CloudShell |
| **`GO2_WORKER_TOKEN`** | Generato dallo script, nel pairing | **SÌ** — è la “password” HTTP tra NX e worker |

**In pratica:**
- Quando hai creato il key pair `LLM_14` in AWS, hai scaricato **una volta** il file `.pem` sul PC. AWS **non** conserva la parte privata.
- Il robot (NX) **non usa** la chiave SSH: parla col worker AWS su **HTTP :8765** con il **token** nel pairing.
- Lo script CloudShell ora fa tutto via **user-data** (script all’avvio EC2): **niente upload .pem in CloudShell**.

Se non hai mai creato `LLM_14` in eu-north-1: **non importa** per questo setup. La macchina parte lo stesso.

---

## COMANDO UNICO (incolla in CloudShell)

```bash
export AWS_REGION=eu-north-1
export GO2_REPO_URL="https://github.com/gpittonMeko/mujoco_go2_d1.git"
git clone --depth 1 "$GO2_REPO_URL" ~/mujoco_go2_d1
bash ~/mujoco_go2_d1/external/openvla_worker/aws/bootstrap-cloudshell.sh
```

**Repo privato** — aggiungi prima:

```bash
export GITHUB_TOKEN="ghp_TUO_TOKEN"
```

**Senza git** — carica `mujoco_go2_d1.zip` in CloudShell, poi:

```bash
export AWS_REGION=eu-north-1
bash ~/mujoco_go2_d1/external/openvla_worker/aws/bootstrap-cloudshell.sh
```

---

## Cosa fa (automatico)

1. Crea **g5.xlarge** in eu-north-1  
2. All’avvio EC2: clone repo + Docker + worker VLA (stub)  
3. Verifica **GET /health** + **POST /plan**  
4. Stampa **`go2-vla-pairing.env`** → copialo e scrivi a Cursor **“sono pronto”**

Log CloudShell: `~/go2-vla-cloudshell.log`  
Log EC2: `/var/log/go2-vla-userdata.log` (via Console → Get system log)

---

## SSH dal PC (solo debug, opzionale)

Se hai `LLM_14.pem` e il key pair è sulla macchina:

```bash
ssh -i ~/Documents/LLM_14.pem ubuntu@<IP_EC2>
```

Non serve per far funzionare il cane.

# Checklist laboratorio — Stagista Dashboard Go2 + D1

**Obiettivo:** capire come funziona la pipeline (camere → piano → braccio) **senza** muovere il robot senza supervisione.

**Prima di iniziare**
- PC collegato alla rete del Go2 (es. `192.168.123.x`).
- URL Jetson: **`http://192.168.123.18:5052`**

### Comando che funziona SEMPRE (copia e incolla)

Da **root** del repo:

```powershell
cd C:\Users\MEKO\Documents\mujoco_go2_d1
.\lab_check.ps1
```

Se sei già in **`scripts\`** (come spesso succede):

```powershell
.\lab_check.ps1
```

Non serve scrivere `python scripts\...` — lo script trova i file da solo.

### Se usi `python` a mano

| Cartella corrente | Comando CORRETTO |
|-------------------|------------------|
| `mujoco_go2_d1\` (root) | `python scripts\verify_dashboard_http.py http://192.168.123.18:5052` |
| `mujoco_go2_d1\scripts\` | `python verify_dashboard_http.py http://192.168.123.18:5052` |

**SBAGLIATO** da `scripts\`: `python scripts\verify_dashboard_http.py` → errore `scripts\scripts\...`

---

## Parte A — Verifiche da terminale (solo lettura)

### Step 1 — La dashboard è accesa?

**Cosa impari:** Flask sulla NX espone `/api/health`; senza questa risposta il browser e tutti gli altri test falliscono.

**Comando:**
```powershell
python scripts\verify_dashboard_http.py http://192.168.123.18:5052
```

**Cosa fare:** annota `pid` e `process_started_at` se vedi `OK`.

**Se fallisce:** controlla cavo/Wi‑Fi, IP Jetson, che qualcuno abbia avviato la dashboard sulla NX.

---

### Step 2 — Le API principali rispondono?

**Cosa impari:** oltre a “viva”, servono endpoint per Sport env, stato generale, ecc. (nessun movimento).

**Comando:**
```powershell
python scripts\verify_go2_lab.py dashboard-nx http://192.168.123.18:5052
```

**Cosa fare:** segna quali righe sono `OK` e quali `FAIL`.

---

### Step 3 — Percorso guidato automatico (11 step spiegati)

**Cosa impari:** ordine logico Scene → 3D → Calib → Mission → Presa → Hermes → YOLO; il tool fa solo `GET` sicuri.

**Comando:**
```powershell
python scripts\stagista_lab_percorso.py http://192.168.123.18:5052
```

**Cosa fare:** apri e leggi:
- `data\stagista_lab_report.json` (risultati numerici)
- `docs\STAGISTA_PERCORSO_DASHBOARD.md` (testo lungo)

**Esito atteso:** `reachable=True ok=9 fail=0` se la NX è raggiungibile.

---

### Step 4 — Smoke veloce “tutto insieme”

**Cosa impari:** entry point unico del laboratorio per controlli rapidi.

**Comando:**
```powershell
python scripts\verify_go2_lab.py quick http://192.168.123.18:5052
```

---

### Step 5 — Hermes (agente linguaggio) senza eseguire motori

**Cosa impari:** Hermes traduce italiano → JSON intent; lo smoke HTTP verifica che l’endpoint esista (può dare 503 se manca la API key — è normale in alcuni deploy).

**Comando:**
```powershell
python scripts\verify_go2_lab.py hermes --http --url http://192.168.123.18:5052
```

**Cosa fare:** se 503 `missing_OPENAI_API_KEY`, segnalalo al tutor; **non** inserire chiavi nel repo.

---

### Step 6 — Grasp Coach (solo stato API)

**Cosa impari:** coach OpenAI per recovery presa; lo status dice se è abilitato.

**Comando:**
```powershell
python scripts\verify_go2_lab.py grasp-coach http://192.168.123.18:5052
```

**Opzionale (solo con tutor, può costare token OpenAI):**
```powershell
python scripts\verify_go2_lab.py grasp-coach http://192.168.123.18:5052 --step
```

**Importante:** **non** aggiungere `--execute`.

---

### Step 7 — Scena 3D del braccio (FK, feedback giunti)

**Cosa impari:** come la dashboard legge angoli servo e costruisce la scena per il viewer.

**Comando:**
```powershell
python scripts\verify_go2_lab.py arm scene3d --base http://192.168.123.18:5052
```

**Cosa fare:** confronta `joints_deg` con quello che vedi nel tab **3D** del browser.

---

### Step 8 — Tre URL da aprire nel browser (JSON)

**Cosa impari:** leggere risposte grezze come fa il frontend.

| URL | Cosa contiene |
|-----|----------------|
| `http://192.168.123.18:5052/api/health` | Servizio, PID |
| `http://192.168.123.18:5052/api/mission/console` | Env, worker grasp, stack NX |
| `http://192.168.123.18:5052/api/arm/grasp_pipeline` | Ordine narrativo: VLA → calib → movimento |

**Cosa fare:** in mission console annota (sì/no, senza copiare segreti): `GO2_ENABLE_REAL_ARM`, `GO2_ANYGRASP_WORKER_URL` presente, grasp worker reachable.

---

## Parte B — Console Operator nel browser

Apri: **http://192.168.123.18:5052/operators**

### Step 9 — Tab Scene (camere)

**Cosa impari:** **log.0** = camera polso (Orbbec), **log.6** = frontale (RealSense). Gli stream MJPEG sono serviti dalla NX.

**Cosa fare:**
1. «Aggiorna stato» → leggi warning su log.0/log.6.
2. Verifica che le anteprime MJPEG non siano nere.
3. «Ricarica stream MJPEG» se il feed è bloccato.
4. (Opzionale) scrivi una nota in **Memoria missione** (titolo + tag).

**Non fare:** cambiare device con ◀▶ senza il tutor.

---

### Step 10 — Tab 3D (viewer)

**Cosa impari:** polling di `/api/arm/scene_3d` → mesh + cilindri FK; **nessun comando motore**.

**Cosa fare:**
1. «Avvia aggiornamento».
2. Prova **fast** vs **full**.
3. Attiva «Cilindri FK» e confronta con `joints_deg` dello Step 7.
4. «Ferma» quando hai finito (risparmia CPU sulla NX).

---

### Step 11 — Tab Stato (mission console)

**Cosa impari:** fotografia del sistema prima di qualsiasi test “serio”.

**Cosa fare:**
1. «Aggiorna» mission console.
2. Attiva «Auto ogni 8s» per 2–3 minuti e osserva cosa cambia.
3. «Aggiorna stack».

**Non fare:** riavvio dashboard / token admin.

---

### Step 12 — Tab Presa (solo osservazione)

**Cosa impari:** pipeline **Piano VLA** (cloud) separata da **esecuzione** (braccio reale).

**Cosa fare:**
1. Solo **Health worker** → leggi se il worker AWS/RTX risponde.
2. Leggi anteprima polso e card testo (non premere esecuzione).
3. Leggi il riquadro fasi (`graspPhase`) dopo health.

**Non fare:** Piano VLA 1 click, Solo POST plan (costo GPU), Sequenza presa, Muovi IK/FK, Avvia/Stop EC2, Grasp Coach con «Esegui mossa D1» attivo.

---

### Step 13 — Tab Hermes (preview)

**Cosa impari:** LLM propone intent JSON; in **preview** nulla viene applicato al cane/braccio.

**Cosa fare:**
1. Seleziona modalità **preview**.
2. Scrivi: *«Descrivi cosa vedi sulle camere e proponi un piano senza eseguirlo»*.
3. Apri **Technical JSON** e individua campi tipo `base_motion`, `arm_joint_delta`, `arm_tool_target`.
4. **Non** confermare esecuzione.

---

### Step 14 — Tab Robot (YOLO 2D)

**Cosa impari:** rilevamento oggetti 2D (box in pixel) senza piano 3D né movimento.

**Cosa fare:**
1. «Rilevamento 2D su log.6».
2. Copia 2–3 box dal pannello testo (classe, score).
3. Vai su tab Scene e verifica visivamente se le box hanno senso.

---

## Parte C — Compito da consegnare al tutor

Compila una pagina (Word/Notion) con:

1. Esito Step 1–4 (OK/FAIL + screenshot terminale).
2. Tabella env da mission console (solo flag, no token).
3. Stato camere log.0 / log.6 (OK / warning / nero).
4. `joints_deg` da Step 7 vs impressione tab 3D.
5. Un paragrafo: *«Differenza tra POST /api/grasp/plan e esecuzione a fasi»* (usa `docs/STAGISTA_PERCORSO_DASHBOARD.md` come riferimento).
6. Screenshot Hermes preview + JSON intent (senza dati sensibili).

---

## Zona rossa — vietato senza tutor presente

| Area | Rischio |
|------|---------|
| Tab **Moto** — stick, slider braccio live, Salva ZERO/START, Home | movimento Go2/D1 |
| Tab **Calib** — «Calibra», «Cancella file» | file calibrazione |
| Tab **Presa** — esecuzione IK/FK, fasi, EC2 | costi + collisioni |
| `verify_go2_lab.py arm move` | movimento braccio via HTTP |
| Riavvio dashboard | interruzione servizio |

---

## Riferimenti rapidi

| File | Uso |
|------|-----|
| `docs/STAGISTA_PERCORSO_DASHBOARD.md` | Spiegazione step + diagramma |
| `data/stagista_lab_report.json` | Ultimo report probe |
| `scripts/verify/README.md` | Altri subcomandi `verify_go2_lab` |

**Tutor — rigenerare report in lab:**
```powershell
cd <root-repo>
python scripts\stagista_lab_percorso.py http://192.168.123.18:5052
```

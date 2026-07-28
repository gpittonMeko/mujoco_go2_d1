# Sandy — solo presa 6D (fazzoletti / pezzi)

Leggi **solo questo**. Ignora MuJoCo, 5050, Hermes, 2D legacy.

---

## Cosa stai facendo

Il cane **Go2** ha un braccio **D1** e una camera 3D al polso.

Deve **prendere un pezzo sul tavolo** (es. pacchetto Tempo) al **centro**, in modo ripetibile.

### Già funziona
Sequenza automatica: guarda (SCAN) → riconosce → prende → rilascia → ripete.

### Il problema
La pinza arriva con angolo spesso ok, ma il **punto di chiusura è sbagliato**:
- troppo **a sinistra/destra**
- troppo **in alto** sul pacchetto (non a metà)
- a volte un po’ sopra/sotto

**Obiettivo:** capire perché e correggere, fino a prese al centro stabili.

---

## Come apri la dashboard (importante)

URL:

```text
http://192.168.123.18:5056/
```

La dashboard è già in ascolto su **tutta la rete locale** del robot (`0.0.0.0:5056`).  
**Non** è su Internet pubblico: serve essere sulla **stessa rete del cane**.

### Se non si apre (caso tipico del collega)

La dashboard sul cane **non è su Internet**. Serve una di queste:

**A) Stessa rete del cane** (Ethernet/switch Unitree `192.168.123.x`)  
→ apri `http://192.168.123.18:5056/`

**B) Proxy sul PC lab** (Matteo lascia acceso questo comando sul PC già collegato al cane):

```powershell
python scripts/proxy_d1_dashboard_lan.py
```

Poi il collega apre **una** di queste (stesso Wi‑Fi / ZeroTier del PC lab):

```text
http://192.168.10.58:5056/          ← Wi‑Fi del PC lab (esempio attuale)
http://10.208.170.235:5056/         ← ZeroTier del PC lab (se siete sulla stessa rete ZT)
```

**C) Check**

```powershell
ping 192.168.123.18
# fallisce → non sei sulla rete cane; usa B) oppure entra in 192.168.123.x
```

Pulsante principale in pagina: **AVVIA CICLO SCAN → GRASP**  
(STOP CICLO + HOLD sempre a portata di mano.)

---

## Sicurezza (30 secondi)

- Prima di restart software: braccio in **fold** (ripiegato) + **HOLD** + qualcuno che lo sostiene.
- HOLD = braccio rigido, non deve cadere.
- Area libera quando lanci il ciclo.

---

## Flusso utile (solo presa)

1. HOLD attivo.
2. Vai in **SCAN** (posa da cui guarda il tavolo).
3. Metti il pezzo sul tavolo.
4. **Riconosci oggetto** (o lancia il ciclo verde).
5. Guarda dove sbaglia (sinistra / destra / alto) e annota.

Calibrazione hand-eye (AprilGrid, ID lab tipico **312**) serve se sospetti errore sistematico camera↔braccio.  
Oggetti **dopo** la calib, non sopra la griglia.

Dettaglio tecnico curl (opzionale): `docs/D1_GRASP6D_NX.md`.

---

## Dove intervenire nel codice

| Cosa | File |
|------|------|
| Pianificazione presa / offset | `go2_dashboard/d1_jog/grasp6d.py` |
| API + ciclo SCAN→grasp | `go2_dashboard/d1_jog/app.py` |
| Camera polso | `go2_dashboard/d1_jog/wrist_rgbd.py` |
| Manopole lab (bias, TCP…) | `scripts/nx_d1_jog_env.sh` |
| UI | `templates/d1_jog_dashboard.html` |

Branch: `_sandy_fix_6d_grasping`

```bash
git fetch origin
git checkout _sandy_fix_6d_grasping
```

---

## Offset / manopole (stato lab)

Frame base braccio: **+Y = sinistra**, **−Y = destra**.

| Variabile | Ora | Nota |
|-----------|-----|------|
| `D1_GRASP6D_FORCE_BIAS_Y_M` | `−0.020` | +2 cm a **destra** del target stimato |
| `D1_GRASP6D_FORCE_BIAS_Z_M` | `0` | alto/basso extra |
| `D1_GRASP6D_TCP_Y_M` | `0.015` | (i 50 mm spingevano fuori a destra) |
| `D1_GRASP6D_PACK_CONTACT_DEEPER_M` | `0` | disattivato |

Il ciclo verde **riplanifica sempre** e usa questi offset (non riusa un piano pregrasp vecchio).

Metodo: **una manopola alla volta**, 3–5 prove uguali, annota.

---

## Cosa consegnare

- Prese al **centro** ripetibili sul pezzo
- Spiegazione: calib? TCP? stima cuboide? bias?
- Valori finali + perché

Se sei perso: apri 5056 → solo **Riconosci oggetto** → scrivi “sbaglia di X cm verso …” → poi tocca una sola variabile.

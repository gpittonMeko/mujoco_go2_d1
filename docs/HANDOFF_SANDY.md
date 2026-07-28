# Sandy — cane Go2 + presa 6D (solo questo)

Ignora MuJoCo, dashboard 5050, Hermes, presa 2D.  
Lavora **solo** sul braccio D1 e sulla presa dei pezzi.

---

## 1. Come collegarti al cane (adesso)

Il computer del robot (Jetson NX) è:

```text
192.168.123.18
```

Dashboard presa:

```text
http://192.168.123.18:5056/
```

### Rete giusta
Devi essere sulla rete Ethernet Unitree / switch del cane:

- IP del tuo PC tipo `192.168.123.x` (es. lab: `192.168.123.50`)
- **Non basta** solo Wi‑Fi ufficio (`192.168.10.x`) né solo ZeroTier

### Check veloce (PowerShell)

```powershell
ping 192.168.123.18
# deve rispondere

# poi apri il browser:
# http://192.168.123.18:5056/
```

Se ping fallisce: cavo Ethernet staccato / PC non sulla rete `192.168.123.x`.

### In pagina
Pulsante grande verde: **AVVIA CICLO SCAN → GRASP**  
Sempre a portata: **STOP CICLO + HOLD**

---

## 2. Cosa stai facendo

Il cane **prende un pezzo sul tavolo** (es. Tempo) con camera al polso + braccio D1.

- La sequenza **già gira**: SCAN → riconosci → prendi → rilascia
- Il problema: chiude **fuori centro** (spesso sinistra / troppo in alto)

**Obiettivo:** prese al **centro** del pezzo, ripetibili.

---

## 3. File da toccare (pochi)

Branch:

```bash
git fetch origin
git checkout _sandy_fix_6d_grasping
```

| File | A cosa serve |
|------|----------------|
| `go2_dashboard/d1_jog/grasp6d.py` | dove calcola il punto di presa / offset |
| `go2_dashboard/d1_jog/app.py` | API + ciclo SCAN→grasp→rilascio |
| `scripts/nx_d1_jog_env.sh` | manopole lab (bias Y/Z, TCP, ecc.) |
| `templates/d1_jog_dashboard.html` | UI 5056 (pulsanti ciclo) |
| `docs/HANDOFF_SANDY.md` | questa guida |

Opzionale camera: `go2_dashboard/d1_jog/wrist_rgbd.py`  
Opzionale tecnico curl: `docs/D1_GRASP6D_NX.md`

---

## 4. Manopole utili (stato ora)

Frame braccio: **+Y = sinistra**, **−Y = destra**.

| Variabile in `nx_d1_jog_env.sh` | Valore ora | Significato |
|--------------------------------|------------|-------------|
| `D1_GRASP6D_FORCE_BIAS_Y_M` | `-0.060` | sposta il target di **6 cm a destra** |
| `D1_GRASP6D_FORCE_BIAS_Z_M` | `0` | alto/basso |
| `D1_GRASP6D_TCP_Y_M` | `0.015` | offset pinza (non rimettere a 0.050) |

Dopo aver cambiato `nx_d1_jog_env.sh` o il codice Python serve **deploy** sulla NX (chiedi in lab: fold + HOLD prima del restart).

Metodo: **una modifica alla volta**, 3–5 prove, annota “ancora X cm a sinistra/destra/alto”.

---

## 5. Sicurezza

- HOLD = braccio rigido (non deve cadere)
- Prima di restart 5056: braccio in **fold** + sostenuto
- Area libera quando lanci il ciclo

---

## 6. Flusso di lavoro tipico

1. Ping ok → apri `http://192.168.123.18:5056/`
2. HOLD ok → pezzo sul tavolo
3. Ciclo verde **oppure** Riconosci oggetto
4. Annota l’errore in cm
5. Tocca **un solo** file/manopola tra quelli sopra
6. Deploy + riprova

# Guida per Sandy — di cosa stiamo parlando (partendo da zero)

Questa guida è per chi **non ha ancora lavorato** su questo robot.  
Leggila tutta una volta. Poi usa l’indice per tornare ai pezzi che ti servono.

Se qualcosa è scritto in linguaggio da “informatici”, qui sotto c’è anche la versione semplice.

---

## 1. Cos’è questo progetto (in 30 secondi)

Abbiamo un **cane robot Unitree Go2** con un **braccio robotico D1** montato sopra.

Al polso del braccio c’è una **telecamera 3D** (RealSense D456): vede colori + distanza.

Il sogno: il braccio **guarda un oggetto sul tavolo**, capisce dove sta nello spazio, e lo **prende con la pinza**.

Oggetto tipico di prova: un **pacchetto di fazzoletti Tempo** (un cubetto piatto sul tavolo).

---

## 2. Il tuo compito (la cosa importante)

### Cosa funziona già
Il robot **sa già fare** una sequenza completa:

1. va in posizione di sguardo (la chiamiamo **SCAN**),
2. riconosce l’oggetto con la camera,
3. prova a prenderlo,
4. lo alza e lo rilascia,
5. torna a guardare.

Quindi **non** ti chiediamo di inventare da zero un robot che prende cose.

### Cosa non va bene ancora
Quando prende il fazzoletto, spesso **non lo prende al centro**.

Esempi di errori tipici:
- chiude **troppo in alto** sul pacchetto (sulla “cima”, non a metà spessore);
- oppure un po’ **di lato**;
- a volte un po’ **sopra** o **sotto** rispetto al punto giusto.

Di solito la **pinza è orientata bene** (guarda dall’alto, lato corto), ma il **punto dove chiude** è sbagliato di qualche centimetro.

### Cosa ti chiediamo di fare
Capire **perché** il punto di presa è spostato e **sistemarlo**, in modo che prenda **sempre al centro** del pacchetto.

Non serve riscrivere tutto. Serve:
- capire la catena “camera → punto nello spazio → braccio”,
- fare prove ripetute,
- correggere calibrazione e/o offset,
- documentare cosa hai cambiato e perché.

---

## 3. Parole utili (glossario espresso)

| Parola | Significato semplice |
|--------|----------------------|
| **Braccio D1** | Il braccio robot sul cane |
| **Pinza / gripper** | Le due “dita” che chiudono sull’oggetto |
| **TCP** | Il punto di riferimento della pinza nel software (non sempre coincide col punto fisico dove le dita toccano) |
| **SCAN** | Posa salvata da cui il braccio **guarda** il tavolo con la camera |
| **HOLD** | Il braccio resta **rigido** nella posa attuale (non cade). Fondamentale per la sicurezza |
| **Couple** | “Accendi” il controllo motore del braccio |
| **Hand-eye** | Calibrazione che dice: “se la camera vede il marker lì, dove sta rispetto al braccio?” |
| **AprilGrid** | Foglio con tanti quadratini/marcatori sul tavolo, usato per calibrare |
| **Preview** | “Dimmi dove prenderesti”, **senza** muovere ancora il braccio alla cieca |
| **Execute / ciclo** | Movimento vero di presa (e nel ciclo: prendi → rilascia → ripeti) |
| **Offset** | Errore di posizione: “volevo il centro, sono andato 2 cm a sinistra” |
| **NX / Jetson** | Il computer sul robot (`192.168.123.18`) |
| **Porta 5056** | L’interfaccia web del braccio + presa 6D (quella che usi tu) |
| **Porta 5050** | Altra dashboard del cane — **non** è quella della presa dal polso |
| **Deploy** | Copiare il codice dal PC al computer del robot |

---

## 4. Sicurezza (leggi prima di toccare qualsiasi cosa)

Il braccio ha peso. Se perdi il controllo (HOLD spento / restart sbagliato) **può cadere**.

### Regole d’oro
1. Prima di riavviare il software del braccio: mettilo in **posa sicura** (braccio ripiegato / “folded”) e **sostienilo con le mani** se serve.
2. Controlla sempre che **HOLD sia attivo** (`hold_active = true`).
3. Non lasciare oggetti o mani nella zona di lavoro quando lanci un ciclo automatico.
4. Hai sempre un pulsante **STOP CICLO + HOLD** nell’interfaccia: usalo se qualcosa va storto.

### Posa sicura (folded)
È la posa “riposata” del braccio, tipo:
- spalla/gomito ripiegati (all’incirca J1 ≈ −90°, J2 ≈ +90°).

Nell’API si chiama spesso `true_zero` / `goto_zero`.  
**Non** confonderla con altri “zero” vecchi del software.

---

## 5. Come apri il progetto e l’interfaccia

### Sul PC (studiare il codice)
```bash
git fetch origin
git checkout _sandy_fix_6d_grasping
```

Poi leggi soprattutto:
1. **questo file** (`docs/HANDOFF_SANDY.md`) ← sei qui
2. `docs/D1_GRASP6D_NX.md` (passi più tecnici)
3. cartella codice presa: `go2_dashboard/d1_jog/`  
   file principali: `app.py`, `grasp6d.py`, `wrist_rgbd.py`, `pick_preset.py`
4. impostazioni laboratorio: `scripts/nx_d1_jog_env.sh`

### Sul robot (provare in lab)
Apri il browser (PC in rete col cane):

**http://192.168.123.18:5056/**

Qui trovi l’interfaccia Teach / PRESA 6D.  
In alto c’è un pulsante verde grande:

**AVVIA CICLO SCAN → GRASP**

Quello fa: vai a SCAN → riconosci → prendi → rilascia dall’alto → torna a SCAN.

---

## 6. Come funziona la presa (storia semplice)

Immagina tre passaggi:

```
Camera al polso  →  “l’oggetto è qui nello spazio”  →  braccio va lì e chiude
```

1. **Calibrazione hand-eye**  
   Insegni al software il legame tra camera e braccio, usando il foglio AprilGrid sul tavolo.

2. **Riconoscimento oggetto**  
   Dalla camera stima un “scatola 3D” (posizione + dimensioni del fazzoletto).

3. **Pianificazione presa**  
   Calcola dove mettere la pinza (di solito dall’alto, chiudendo sul lato corto) e muove il braccio.

Se uno di questi tre pezzi è storto, la pinza arriva “quasi bene” ma **non al centro**.

---

## 7. Calibrazione hand-eye (oggi, passo passo)

Serve **prima** di pretendere prese precise.

1. Metti sul tavolo il foglio **AprilGrid** (nel nostro lab i tag partono spesso dall’ID **312**).
2. **Togli** il fazzoletto dal foglio: durante la calibrazione la griglia deve essere libera.
3. Accendi il controllo braccio (**couple + HOLD**).
4. Nell’interfaccia apri **Calibrazione hand-eye**.
5. Raccogli tanti **sample** da posizioni diverse:
   - a mano (muovi, HOLD, salva), oppure
   - **AUTO** (il braccio si muove da solo — area libera, mano pronto sullo stop).
6. Premi **Calcola hand-eye**.
7. Controlla che risulti **ok**. Se il “residuo” (errore) è alto, la calibrazione è scarsa: non fidarti del millimetro.
8. **Poi** rimetti il fazzoletto e passa alla presa.

Nota: i file di calibrazione vivono sul computer del robot (`data/d1_grasp6d_*.json`).  
**Non** arrivano automaticamente clonando GitHub. Se ricalibri, lo fai sul robot.

---

## 8. Il problema sul fazzoletto (cosa abbiamo già visto)

### Sintomo
Da sopra il pacchetto, la pinza chiude **troppo in alto** (sulla parte alta / faccia superiore) invece che **a metà** del pacchetto, e a volte non è **centrale** in pianta.

### Cose già provate (patch)
Nel tempo sono stati messi degli “aggiustamenti a mano” nelle variabili d’ambiente, per esempio:
- spostare il TCP (soprattutto Y ≈ 50 mm),
- bias in altezza,
- chiusura pinza più ferma,
- ciclo che rilascia dall’alto.

Queste cose a volte **migliorano** una prova, ma **non dimostrano** che la calibrazione sia giusta.  
Il tuo lavoro è arrivare a una spiegazione pulita + correzione stabile.

### Come investigare (metodo da studente serio)
Fai **una modifica alla volta**, 3–5 prove uguali, annota.

1. Guarda se la calibrazione hand-eye è buona (residuo basso, tanti sample diversi).
2. Fai **Riconosci oggetto** / preview e confronta:
   - dove il software *pensa* di prendere,
   - dove il braccio *va davvero* (foto `debug.jpg`, occhio in lab).
3. Se l’errore è **sempre sullo stesso asse** → sospetta TCP / offset pinza.
4. Se l’errore **cambia** spostando oggetto o posa → sospetta calibrazione o stima del cuboide (depth vs RGB).
5. Non girare tre manopole insieme: altrimenti non sai cos’ha funzionato.

---

## 9. Manopole utili (variabili) — solo le importanti

Stanno in `scripts/nx_d1_jog_env.sh` (sul robot possono essere sovrascritte).

| Variabile | In italiano |
|-----------|-------------|
| `D1_GRASP6D_TCP_X/Y/Z_M` | Dove il software crede stia il “centro pinza” rispetto al polso |
| `D1_GRASP6D_PACK_GRASP_HEIGHT_FRAC` | A che altezza del pacchetto chiudere (`0.5` = metà spessore) |
| `D1_GRASP6D_PACK_CONTACT_DEEPER_M` | Quanto “entrare” nel pacchetto durante l’avvicinamento |
| `D1_GRASP6D_FORCE_BIAS_Z_M` | Spostamento alto/basso extra (il vecchio `+0.010` alzava troppo sul Tempo) |
| `D1_GRASP6D_FIRM_CLOSE_MAX_DEG` | Quanto chiudere forte la pinza |
| `D1_GRASP6D_APRILGRID_FIRST_ID` | Primo ID della griglia (lab tipico: `312`) |

Se non capisci una variabile: **non toccarla** finché non hai letto dove viene usata in `grasp6d.py`.

---

## 10. Flusso pratico in laboratorio

### A) Solo studiare / riconoscere (poco rischio)
1. HOLD attivo.
2. Vai in SCAN (pulsante “Vai alla SCAN”).
3. Metti il Tempo sul tavolo.
4. **Riconosci oggetto** e guarda il debug.
5. Non lanciare ancora il ciclo se la proposta sembra assurdamente fuori posto.

### B) Una presa o un ciclo
1. Area libera.
2. HOLD ok.
3. Pulsante verde **AVVIA CICLO SCAN → GRASP** (o grasp singolo).
4. Mano pronta su **STOP CICLO + HOLD**.

### C) Dopo aver cambiato codice sul PC
Per vedere le modifiche sul robot serve un **deploy** dello script D1:

`python scripts/deploy_d1_jog_to_nx.py`

Di default **copia solo i file** (non riavvia).  
Se serve riavvio della dashboard 5056:
- braccio in posa sicura,
- **sostieni** il braccio,
- conferma esplicita di sicurezza,
- poi restart con i flag documentati nello script.

**Non** usare per la presa il deploy della dashboard 5050.

---

## 11. Dove sta il codice (e cosa ignorare)

### Guarda qui
| Cosa | Dove |
|------|------|
| Logica presa 6D | `go2_dashboard/d1_jog/` |
| Pagina web UI | `templates/d1_jog_dashboard.html` |
| Impostazioni lab | `scripts/nx_d1_jog_env.sh` |
| Deploy braccio/presa | `scripts/deploy_d1_jog_to_nx.py` |
| Questa guida | `docs/HANDOFF_SANDY.md` |

### Per ora puoi ignorare
- simulatore MuJoCo (`unitree_mujoco/`)
- cartelle `old/`, `cyclonedds/`
- foto/json di debug sparsi in root (sono di sessione, non codice)

---

## 12. Branch Git

- `main` — stato di consegna
- `_sandy_fix_6d_grasping` — branch di lavoro per te (parte dallo stesso punto)

Lavora sul branch Sandy, fai commit chiari, spiega il *perché* nei messaggi.

---

## 13. Checklist primo giorno

1. [ ] Clone/pull del branch `_sandy_fix_6d_grasping`
2. [ ] Letto questo file per intero
3. [ ] Aperto `http://192.168.123.18:5056/` (se sei in lab)
4. [ ] Capito cos’è HOLD e cos’è la posa sicura
5. [ ] Capito il tuo obiettivo: **centro del fazzoletto, ripetibile**
6. [ ] Fatto una preview / riconoscimento senza muovere alla cieca
7. [ ] Annotato dove sbaglia (alto / lato / basso) su 3 prove
8. [ ] Non committare jpg/json di prova in root

---

## 14. Cosa consegnare alla fine (obiettivo qualità)

Non basta “a occhio una volta ha preso”.

Vogliamo:
- prese al **centro** del Tempo in modo **ripetibile**,
- spiegazione: era calibrazione? TCP? stima cuboide?,
- valori finali delle manopole + motivo,
- magari 2–3 screenshot/debug che mostrano prima/dopo.

---

## 15. Se sei perso: da dove ripartire

1. Rileggi il §2 (il tuo compito).
2. Apri la UI 5056 e fai solo **Riconosci oggetto**.
3. Confronta con quello che vedi con gli occhi.
4. Scrivi in una riga: “sbaglia di X cm verso …”  
5. Solo dopo tocca calibrazione o una variabile.

Se hai dubbi di sicurezza sul braccio: **STOP / HOLD**, sostieni, chiedi in lab. Meglio una domanda in più che un braccio a terra.

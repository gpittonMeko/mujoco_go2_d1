# Come si comanda il robot (Go2) in questo laboratorio

Documento per **studenti**: cosa succede “sotto il cofano”, in che **ordine** avviare i programmi e **quali comandi** usare. Si riferisce al progetto **Unitree_Simulator** (MuJoCo + DDS, stessa architettura del robot reale a livello di messaggi).

---

## 1. Idea generale: non c’è un “telecomando magico”

In simulazione (e in low-level sul robot reale) il movimento non è “vai avanti” come su un’auto radiocomandata ad alto livello. Qui si parla di **controllo di basso livello**:

- Il simulatore **legge** un messaggio chiamato **LowCmd** (comandi per ogni motore: posizione desiderata, guadagni, coppia, ecc.).
- Il simulatore **pubblica** un messaggio **LowState** (stato reale dei motori, IMU, ecc.).

I programmi che “comandano” il robot sono **processi separati** che:

1. si collegano alla rete DDS (in laboratorio spesso sull’interfaccia di loopback `lo` o sulla LAN);
2. **sottoscrivono** `rt/lowstate` per sapere come è il robot;
3. **pubblicano** `rt/lowcmd` con i comandi calcolati (policy RL, pose fisse, slider del braccio, …).

**Regola d’oro per gli studenti:** prima parte il **simulatore** (`unitree_mujoco.py`), poi lo script che invia i comandi. Se invertite l’ordine, lo script resta in attesa dello stato (“In attesa di lowstate…”).

---

## 2. Vocabolario minimo

| Termine | Significato |
|--------|-------------|
| **DDS** | Middleware di comunicazione tra processi (CycloneDDS). Topic come `rt/lowcmd` e `rt/lowstate`. |
| **Domain ID** | “Canale” logico: in **simulazione** si usa di solito **1**; sul robot reale spesso **0**. Tutti i programmi devono essere d’accordo. |
| **LowCmd** | Pacchetto con comandi per fino a 20 motori (nel Go2 “plain” se ne usano 12; con braccio Z1/D1 in modello `go2_d1` anche i giunti del braccio). |
| **LowState** | Pacchetto con lo stato corrente (posizioni, velocità, sensori). |
| **Policy RL** | Rete neurale pre-addestrata che, dato lo stato e un comando di velocità desiderata, calcola le posizioni dei giunti delle **gambe** per camminare. |

---

## 3. Configurazione prima di iniziare

Nel file `unitree_mujoco/simulate_python/config.py` conviene sapere cosa toccare:

- **`ROBOT`** – `"go2"` solo quadrupede; `"go2_d1"` quadrupede + **braccio Z1** (MJCF `go2_d1.xml`, scena `scene.xml`). Il braccio **D1** con mesh `go2_d1_d1mesh.xml` è una variante separata: sim `unitree_mujoco_d1viz.py` + script `run_go2_d1_ball_d1kin.py`, non questo flusso.
- **`DOMAIN_ID`** – per la sim è tipicamente **1** (allineato a come inizializzano gli script Python del progetto).
- **`INTERFACE`** – interfaccia di rete per il multicast DDS. Su molte macchine Linux si usa `"lo"` per sim locale; a volte serve `"lan2"` o altra interfaccia se `lo` dà errori multicast.

**Per gli studenti:** se il sim parte ma gli script non “vedono” il robot, la prima cosa da controllare è **stessa interfaccia** e **multicast su loopback** (vedi troubleshooting in `docs/README.md`).

---

## 4. Ordine di avvio (laboratorio tipo)

1. **Terminale 1 – Simulatore MuJoCo**

   ```bash
   cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py
   ```

   Si apre la finestra MuJoCo; se attiva la depth camera, anche una finestra OpenCV. Con `ROBOT = "go2_d1"` questo è il modello **Z1** sul dorso.

   **Palla / reach autonomo (braccio Z1):** dopo il sim, in un altro terminale: `python3 scripts/run_go2_d1_ball.py --interface lo` (stessa interfaccia DDS del `config.py`). Per il **D1 mesh** non usare questo comando: servono `unitree_mujoco_d1viz.py` e `run_go2_d1_ball_d1kin.py`.

2. **Terminale 2 – Chi comanda le gambe (scegliere UNA modalità)**

   - Policy con velocità **fisse** sulla riga di comando:

     ```bash
     python3 scripts/deploy_policy.py --model ts --vx 0.5 --vy 0.0 --vyaw 0.0
     ```

   - Policy con **joystick da tastiera** (incrementi a ogni pressione di tasto; serve `tkinter`):

     ```bash
     python3 scripts/deploy_policy.py --model ts --joystick
     ```

   - **Joystick virtuale** alternativo (`virtual_joystick/main.py`): stessa policy in thread, ma la tastiera funziona in modalità **tieni premuto** (vedi sezione 6).

---

## 5. Cosa comandiamo con la policy (gambe)

Lo script `scripts/deploy_policy.py`:

- legge `rt/lowstate`;
- costruisce l’osservazione richiesta dal modello (Teacher-Student **ts** o Walk-These-Ways **wtw**);
- esegue l’inferenza PyTorch;
- scrive su `rt/lowcmd` le **posizioni target** per i **12 motori delle gambe** (con guadagni `kp`/`kd` dalla configurazione YAML in `go2_deploy/params/`).

**Comandi di alto livello** che la policy interpreta come “dove vuoi andare”:

- **`vx`** – velocità avanti/indietro (m/s).
- **`vy`** – velocità laterale (m/s).
- **`vyaw`** – velocità di imbardata (rad/s).

Parametri utili da riga di comando:

- `--model ts | wtw` – quale rete usare.
- `--vx`, `--vy`, `--vyaw` – setpoint quando **non** si usa `--joystick`.
- `--joystick` – finestra tastiera: **W/S** `vx`, **A/D** `vy`, **Q/E** `vyaw`, **Spazio** azzera (con incrementi `STEP` ad ogni pressione).
- `--arm-hold` – obbligatorio in pratica per **`go2_d1`**: fissa il braccio in una pose compatta e compensa le gambe (altrimenti il peso del braccio destabilizza la camminata). Sul Go2 senza braccio nel modello non serve.
- `--interface` – sovrascrive l’interfaccia DDS (es. per allinearsi al simulatore o al robot reale).

In simulazione **non** sono disponibili le modalità “Sport” / walk ad alto livello dell’app Unitree: tutto passa da **LowCmd** come sopra.

---

## 6. Due modi di “joystick” a tastiera (attenzione!)

Sono simili ma **non identici**:

| Aspetto | `deploy_policy.py --joystick` | `virtual_joystick/main.py` |
|--------|-------------------------------|----------------------------|
| Comportamento tasti | Ogni pressione **incrementa/decrementa** `vx`, `vy`, `vyaw` (fino a un massimo). | **Tieni premuto** W/A/… per avere velocità al **massimo** consentito; rilasci e torna verso zero (secondo la logica dei tasti). |
| Stop | **Spazio** azzera i setpoint. | **Spazio** resetta i tasti “premuti”. |
| Avvio | Un solo comando: include policy + GUI. | Due processi: sim + `virtual_joystick/main.py`. |

Entrambi pubblicano **LowCmd** tramite la stessa architettura DDS; non avviateli **insieme** sullo stesso topic senza sapere cosa fate (due comandi che si sovrascrivono).

---

## 7. Go2 con braccio (`go2_d1`)

### 7.1 Perché serve `--arm-hold` con la policy

Con il modello `go2_d1`, la rete controlla le gambe ma il braccio ha **6 motori aggiuntivi** (indici 12–17 nel messaggio). Se non li fissate, il braccio può muoversi o caricare male il corpo. **`--arm-hold`** imposta una pose ripiegata stabile e applica un offset alle gambe per ridurre il “lean” in avanti.

Esempio tipico in laboratorio:

```bash
python3 scripts/deploy_policy.py --model ts --vx 0.5 --arm-hold
python3 virtual_joystick/main.py --arm-hold
```

### 7.2 Ordine dei motori (12 + 6)

Per le **gambe**, l’ordine è sempre lo stesso schema Unitree Go2:

- **FR, FL, RR, RL** – per ogni gamba: **hip, thigh, calf** (3 giunti × 4 zampe = 12).

Poi, per `go2_d1`, i **6 giunti del braccio** occupano gli slot successivi nel LowCmd (nel codice: `motor_cmd[12]` … `motor_cmd[17]`).

### 7.3 Controllare solo il braccio: `d1_arm/arm_control.py`

Script con interfaccia a **slider** (e preset Home/Fold) per i giunti del braccio. Invia **LowCmd** con una **posa fissa** delle gambe (stand) e comanda il braccio.

**Uso didattico:**

```bash
# config.py con ROBOT = "go2_d1"
cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py
# Altro terminale:
python3 d1_arm/arm_control.py
```

**Importante:** mentre `arm_control` gira, **non** fate girare contemporaneamente un altro processo che pubblica **lo stesso** `rt/lowcmd` per le gambe (es. `deploy_policy`), perché si pestano i piedi: un messaggio sovrascrive l’altro. In lezione: o **cammino con policy** (`--arm-hold` sul braccio), o **studio braccio fermo** con `arm_control`, a meno di non aver progettato un unico nodo che **fonde** i comandi (non è lo scenario base di questi script).

---

## 8. Script didattico senza policy: `test_movimento.py`

Per vedere **pose predefinite** (alzati / squat / abbassati) senza rete neurale:

```bash
cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py
python3 scripts/test_movimento.py
```

Opzione `--arm-hold` se il modello è `go2_d1`. Utile per capire che si possono inviare **posizioni articolari** dirette oltre alla policy.

---

## 9. Simulatore con gamepad fisico (opzionale)

In `config.py`, `USE_JOYSTICK` e `JOYSTICK_TYPE` / `JOYSTICK_DEVICE` fanno sì che **il simulatore** possa leggere un controller e imitare il Wireless Controller Unitree. È un percorso **diverso** dagli script `deploy_policy` / `virtual_joystick`: lì il comando entra dal sim; negli script Python il comando è calcolato esternamente e inviato via DDS.

---

## 10. Dal simulatore al robot reale (cenni)

- Sul robot si usa in genere **`DOMAIN_ID = 0`** e l’**interfaccia di rete** corretta (es. Ethernet collegata al robot), spesso passando `--interface ...` agli script.
- Stessi **nomi topic** (`rt/lowcmd`, `rt/lowstate`): la logica imparata in sim è trasferibile, ma su hardware valgono **limiti di sicurezza**, batteria, spazio libero e procedure Unitree.
- **Non** fate esperimenti sul robot reale senza supervisione e senza aver compreso arresto emergenza e manuali.

---

## 11. Checklist per lo studente prima della prova

1. `config.py`: `ROBOT` giusto per la lezione (`go2` vs `go2_d1`).
2. Interfaccia DDS e multicast ok (se gli script restano in attesa di `lowstate`, rivedere `docs/README.md` troubleshooting).
3. Avviato **prima** `unitree_mujoco.py`, **poi** lo script di comando.
4. Con `go2_d1` e policy: ricordare **`--arm-hold`**.
5. Un solo “comandante” su `rt/lowcmd` alla volta, salvo progetti avanzati espliciti.

---

## 12. Dove approfondire

- Panoramica tecnica e depth camera: [`docs/README.md`](README.md)
- Braccio, build modello MuJoCo, link commerciali: [`d1_arm/README.md`](../d1_arm/README.md)
- Joystick virtuale (sintesi comandi): [`virtual_joystick/README.md`](../virtual_joystick/README.md)
- Commenti d’uso in cima a: `scripts/deploy_policy.py`, `d1_arm/arm_control.py`

Questo documento non sostituisce i manuali ufficiali Unitree; serve a **inquadrare il flusso di comando** usato in questo repository per le esercitazioni.

# D1 — Fattibilità «accompagnamento» / drag-teaching (vs UR)

Questo repository **non contiene** gli header IDL Unitree (`PubServoInfo_.hpp`, `ArmString_.hpp`) del SDK installato sul robot: sono nel toolchain Jetson sotto i path del pacchetto **unitree_sdk2** (es. `/usr/include` o tree di build locale).

## Cosa è già verificabile dal codice qui

### Comandi (`rt/arm_Command`)

Gli helper [`scripts/d1_arm_dds_helper.cpp`](../scripts/d1_arm_dds_helper.cpp) pubblicano righe JSON via `stdin`. La dashboard usa oggi solo:

- **`funcode` 5** con `data.mode: 1` — inizializzazione / stato controller (come da uso in `publish_d1_hold_current` / `_stage_messages`).
- **`funcode` 2** — comando di **posa** (angoli servo in gradi).

Non è documentato in-repo un valore `funcode`/`mode` ufficiale per **torque/impedenza/drag** tipo UR.

### Feedback

[`scripts/d1_arm_feedback_helper.cpp`](../scripts/d1_arm_feedback_helper.cpp) sottoscrive:

- `current_servo_angle` → messaggio `PubServoInfo_` — lo script stampa **solo sette float** (`servo0_data_` … `servo6_data_`) come `servo_angles`.
- `arm_Feedback` → `ArmString_` — stringa JSON/messaggio testuale (dump grezzo).

Per sapere se `PubServoInfo_` include **coppia/corrente** oltre all’angolo, aprire sul **Jetson** l’header reale, ad esempio:

```bash
# adatta il path alla tua installazione SDK2
find /usr /opt -name 'PubServoInfo_.hpp' 2>/dev/null | head -5
```

### Confronto con UR

Su robot industriali UR la modalità «freedrive» è definita nel controller. Sul D1 i servo sono su **bus dedicato**; il produttore indica «controllo di forza» a livello commerciale, ma **il binding DDS effettivo per drag teaching va confermato** da documentazione o cattura traffico quando l’app ufficiale Unitree è in modalità insegnamento.

## Esiti possibili (come nel piano)

| Esito | Azione in software |
|--------|---------------------|
| **A** — Esiste modalità compliant ufficiale (tabella `funcode`/`mode`) | Aggiungere messaggi in `_run_d1_messages` + route Flask + UI «entra/uscì accompagna». |
| **B** — Coppia in feedback, nessun drag nativo | Valutare controllo in ammettenza (complessità e sicurezza; fuori scope finché non analizzato). |
| **C** — Solo posizione | Workaround: posizionamento manuale con tool vendor o pose intermedie; **Salva START** con angoli letti da feedback (implementato in dashboard). |

## Workaround operativo attuale (C)

1. Mettere il braccio nella regione desiderata (come consentito dal firmware — spesso posa comandata a piccoli step o app Unitree).
2. **Leggi angoli servo** / **Salva START** sulla dashboard: `start_alignment.json` include scena AprilTag **e** snapshot articolare per seed IK.
3. **Mirror (dashboard):** non abbassa la stiffness servo — è solo inseguimento lento della posa letta. Output Python ora appendono anche **`data/drag_follow_process.log`** sul NX (errori tipo feedback assente o PID morto).

Diagnostica strutturata (tail log + JSONL tick `data/drag_follow_diag.jsonl` + hints): dalla dashboard pulsante **Diagnostica completa** oppure `GET /api/arm/drag_follow/diagnostics?servo=1`.

Endpoint diagnostico: `POST /api/arm/teach_mode` restituisce ancora **non implementato** finché la tabella protocollo non è integrata.

### Dopo Stop mirror sulla dashboard

- Di default **`GO2_DRAG_HOLD_AFTER_STOP=1`**: alla fine di `POST /api/arm/drag_follow` con `{ "enable": false }` viene inviato un **`publish_d1_hold_current`** per ripetere la posa servo misurata (riduce creep vs fermarsi senza comandi).
- Disabilitare: `GO2_DRAG_HOLD_AFTER_STOP=0`. Oppure body JSON `{ "enable": false, "hold_after_stop": false }`.

## Prototipo software in-repo: drag-follow (echo / pass-through / mirror / assist)

Non è firmware drag-teach; è un anello che legge `servo_angles` da `bin/d1_arm_feedback_helper` e invia posa (`funcode` 2).

### Echo (default dashboard — «massimo morbido» lato PC)

Ad ogni tick invia gli angoli misurati come comando posizione. Su **spalla/gomito** si applicano più decimali e un piccolo **lead** sul \(\Delta\) feedback tra letture consecutive, perché J1/J2 spesso restavano «congelati» con solo `round(..., 3)` e feedback lento. **Non spegne la coppia** dei servo.

### Pass-through

Filtro esponenziale su \(q_{\mathrm{misurato}}\) + comando che si avvicina con passo massimo per giunto (slew). Utile se l’eco jittera su encoder rumorosi.

### Mirror (legacy)

`q_cmd` viene aggiornato ogni tick con  
`q_cmd += η · (q_{\mathrm{misurato}} - q_{\mathrm{cmd}})` (η piccolo, passo massimo per giunto). Va bene quando la mano sposta gli angoli di **frazioni di grado** (es. confronto riposo / forza leggera / forza alta sullo stesso giunto).

### Assist (alternativa)

Stima \(\Delta q \approx q_{\mathrm{now}} - q_{\mathrm{prev}}\) filtrata e invia  
\(q_{\mathrm{cmd}} = q_{\mathrm{now}} + \mathrm{gain}\cdot \mathrm{clip}(\Delta q)\).

Script CLI: [`scripts/d1_drag_follow_experimental.py`](../scripts/d1_drag_follow_experimental.py) (`--mode echo`, `passthrough`, `mirror`, o `assist`).  
Dashboard: `POST /api/arm/drag_follow` con `enable: true` e `mode`, più parametri frequenza / mirror — `{ "enable": false }` termina il sottoprocesso.

**Rischi:** rumore, oscillazioni, inseguimento spurio — solo area libera e operatore che può fermare (Ctrl+C sullo script, Stop dalla dashboard, kill PID).


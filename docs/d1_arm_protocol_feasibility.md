# D1 — Fattibilità «accompagnamento» / drag-teaching (vs UR)

Questo repository **non contiene** gli header IDL Unitree (`PubServoInfo_.hpp`, `ArmString_.hpp`) del SDK installato sul robot: sono nel toolchain Jetson sotto i path del pacchetto **unitree_sdk2** (es. `/usr/include` o tree di build locale). In repo ci sono copie Cyclone in [`msg/ArmString_.hpp`](../msg/ArmString_.hpp) e [`msg/PubServoInfo_.hpp`](../msg/PubServoInfo_.hpp) per build helper C++.

Riferimento ufficiale: **D1 Mechanical Arm Services Interface** (Unitree Go2 SDK doc) — DDS `rt/arm_Command` / `rt/arm_Feedback`, JSON `seq` / `address` / `funcode` / `data`.

## Mappa protocollo Unitree ↔ questo repository

| Funzione (doc Unitree) | address | funcode | Nel repo | File / nota |
|------------------------|---------|---------|----------|----------------|
| Singolo giunto | 1 | 1 | **No** | `move_one` usa funcode **2** con 7 angoli ([`d1_arm_publish_lite.py`](../go2_dashboard/d1_arm_publish_lite.py)) |
| Tutti i giunti | 1 | 2 | **Sì** | Movimento principale; `data.mode` 0/1 ([`d1_arm_publish_lite.py`](../go2_dashboard/d1_arm_publish_lite.py), [`diagnostics_dashboard.py`](../diagnostics_dashboard.py)) |
| Enable/scarica singolo motore | 1 | 4 | **No** | — |
| Enable/scarica tutti i motori | 1 | 5 | **Parziale** | Solo `mode: 1` prima di ogni burst; **mai** `mode: 0` (drag ufficiale) |
| Alimentazione motori | 1 | 6 | **No** | E-stop HTTP = hold funcode 2, non power off |
| Zero postura (firmware) | 1 | 7 | **No** | Zero lab = `data/true_zero_pose.json` + funcode 2 |
| Angoli giunti (feedback) | 2 | 1 | **Parziale** | Angoli da topic `current_servo_angle` (`PubServoInfo_`), non parse JSON su `arm_Feedback` |
| Stato braccio | 2 | 3 | **No** | `enable_status`, `power_status`, `error_status` non parsati |
| Stato motori online | 2 | 4 | **No** | `motor0_status`…`motor6_status` non parsati |
| Ricezione comando | 3 | 1 | **No** | `recv_status` non usato |
| Esecuzione comando | 3 | 2 | **No** | `exec_status` non usato |

### Topic DDS

| Topic (doc / sample SDK) | Nel repo | File |
|--------------------------|----------|------|
| `rt/arm_Command` | **Sì** | [`scripts/d1_arm_dds_helper.cpp`](../scripts/d1_arm_dds_helper.cpp) → `bin/d1_arm_command` |
| `rt/arm_Feedback` | **Non sottoscritto** | Doc driver; sample #5 usa anche `arm_Feedback` senza `rt/` |
| `arm_Feedback` | **Sì (solo log)** | [`scripts/d1_arm_feedback_helper.cpp`](../scripts/d1_arm_feedback_helper.cpp) |
| `current_servo_angle` | **Sì** | C++ helper, [`d1_arm_servo_read_python.py`](../scripts/d1_arm_servo_read_python.py), [`d1_arm_servo_stream_ndjson.py`](../scripts/d1_arm_servo_stream_ndjson.py) |

### HTTP dashboard operator (porta **5052**, lite)

| Equivalente operativo | Route | Stack DDS |
|-------------------------|-------|-----------|
| Lettura angoli | `GET /api/arm/servo_snapshot` | `read_servo_deg_with_diag` |
| Tutti i giunti | `POST /api/arm/joints/goto_deg`, `live_deg` | funcode 5+2 |
| Un giunto (workaround) | `POST /api/arm/joints/move_one` | funcode 2 (tutti e 7) |
| Zero lab | `POST /api/arm/goto_true_zero`, `true_zero` | file JSON + funcode 2 |
| Hold / e-stop soft | `POST /api/arm/emergency_hold` | funcode 5+2 hold |
| Drag-teach ufficiale | — | `POST /api/arm/teach_mode` → **501** (solo monolite [`diagnostics_dashboard.py`](../diagnostics_dashboard.py)) |
| Drag software | — | `POST /api/arm/drag_follow` → **solo monolite**; lite: [`d1_drag_follow_experimental.py`](../scripts/d1_drag_follow_experimental.py) CLI |

## Cosa è già verificabile dal codice qui

### Comandi (`rt/arm_Command`)

Gli helper [`scripts/d1_arm_dds_helper.cpp`](../scripts/d1_arm_dds_helper.cpp) pubblicano righe JSON via `stdin`. La dashboard usa oggi solo:

- **`funcode` 5** con `data.mode: 1` — **enable tutti i motori** (tabella Unitree); inviato come primo messaggio di ogni burst.
- **`funcode` 2** — comando di **posa** (angoli servo in gradi, `angle0`…`angle6`, `mode` 0 stream / 1 traiettoria).

**funcode 5 `mode: 0`** (scarica coppia, drag memory teaching) non è usato in produzione; vedi `teach_mode` 501 sotto.

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


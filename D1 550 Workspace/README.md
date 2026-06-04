# D1 550 Workspace

Workspace per il braccio Unitree D1 (550) sul progetto Go2.

## Layout

| Percorso | Contenuto |
|----------|-----------|
| `d1_sdk/` | SDK ufficiale / esempi DDS (cartella attiva — non in `OLD`) |
| `OLD/` | Materiale precedente spostato dal repo (helper, mesh, URDF, script lab, dashboard parziale) |

### `OLD/` (archivio)

- `d1_550_description/` — pacchetto URDF ROS
- `d1_arm/` — controllo / note MuJoCo
- `msg/` — stub IDL `ArmString_` / `PubServoInfo_` per build helper C++
- `scripts/` — helper DDS, build, simulazione, kinematics template
- `go2_dashboard/d1_servo_feedback.py`
- `docs/d1_arm_protocol_feasibility.md`
- `unitree_mujoco/` — scena `go2_d1`, mesh, `d1viz`

I path nel monolite sono centralizzati in `go2_dashboard/paths.py` (`D1_*`, `REL_*`). Il deploy NX copia da `OLD/` verso `scripts/`, `msg/`, `unitree_mujoco/...` flat sul robot.

## Dashboard jog (SDK pulito)

Pagina dedicata con slider (non dipende dal monolite `diagnostics_dashboard`):

1. Compila i binari SDK: `bash scripts/build_d1_sdk.sh` → `bin/d1_sdk_command`, `bin/d1_sdk_feedback`
2. Avvia: `python scripts/serve_d1_jog_dashboard.py` → **http://&lt;host&gt;:5053/**
3. Sulla NX o sul controller D1 (`192.168.123.100`): `GO2_LOCAL=1` e stesso `scripts/nx_dashboard_env.sh` per `LD_LIBRARY_PATH` DDS

Protocollo: `funcode` 2 `mode` 0 (jog), 5 enable, 7 zero — vedi `Software Interface Services.docx` / `D1_ARM_SERVICES_EXTRACTED.md`.

### Deploy sulla NX (porta **5053**, indipendente da operator **5052**)

```bash
python scripts/deploy_d1_jog_to_nx.py
# oppure: python scripts/launch_d1_jog_dashboard_nx.py
```

Apri: **http://192.168.123.18:5053/** — non modifica `nx_dashboard_supervise` né `nx_dashboard_env.sh`.

Esperimenti visione YOLO/RealSense sulla stessa porta: branch `archive/vision-yolo-5053`.

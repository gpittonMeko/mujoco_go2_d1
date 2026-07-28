# Documentazione (indice)

Setup sim, policy e struttura repo: **[README nella root](../README.md)**.

Guide operative:

| Documento | Contenuto |
|-----------|-------------|
| [HANDOFF_SANDY.md](HANDOFF_SANDY.md) | **Consegna colleghi** — presa 6D, 5056 vs 5050, deploy, env, sicurezza |
| [D1_GRASP6D_NX.md](D1_GRASP6D_NX.md) | Flusso operativo curl / calib / preview / execute 6D |
| [localDogTest_jetson.md](localDogTest_jetson.md) | Jetson/NX, dashboard (`scripts/serve_dashboard_modular.py`), deploy, presa |
| [comandare_il_robot_per_studenti.md](comandare_il_robot_per_studenti.md) | LowCmd, laboratorio |
| [d1_arm_protocol_feasibility.md](d1_arm_protocol_feasibility.md) | D1 DDS, drag-follow |

Script in `scripts/` — nomi parlanti; deploy dashboard: `python scripts/deploy_dashboard_to_nx.py`.

## Safety restart/handoff D1

- `scripts/launch_d1_jog_dashboard_nx.py` puo' fare restart/handoff del controllo braccio.
- Non eseguire restart con braccio sospeso: sostenere il braccio o metterlo prima in posa sicura.
- Per sola diagnostica usare `python scripts/launch_d1_jog_dashboard_nx.py --status-only`.
- Per restart esplicito usare `python scripts/launch_d1_jog_dashboard_nx.py --confirm-restart-risk`.

## Debug grafico cluster 6D

- JSON debug cluster: `/api/pick/grasp6d/debug`
- Overlay JPEG cluster: `/api/pick/grasp6d/debug.jpg`
- In UI 5056: pulsante `Diagnostica 6D grafica` (tab Teach).

## Sequenza operativa presa 6D

- Durante la calibrazione hand-eye la AprilGrid deve restare libera, ferma e visibile: non lasciare cuboidi o oggetti sopra la griglia.
- Gli oggetti di presa vanno inseriti solo dopo build calibrazione valido (`/api/pick/metric/calibration` con calibrazione `ok`).
- Prima di muovere il braccio: `POST /api/pick/grasp6d/preview`, poi `POST /api/pick/grasp6d/cluster_probe`.
- Se `cluster_probe.summary.ready_for_pregrasp` e' true, procedere con pregrasp; eseguire la presa completa solo dopo conferma visiva e `confirm: "EXECUTE_GRASP6D"`.

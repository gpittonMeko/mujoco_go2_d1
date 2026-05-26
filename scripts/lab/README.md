# Script di laboratorio

Utility CLI per smoke test e cicli presa dalla dashboard operator (porta 5052).
Non fanno parte del runtime Flask sulla NX; sono invocati dal PC in LAN.

| Script | Uso |
|--------|-----|
| `lab_mission_status.py` | `python scripts/lab/lab_mission_status.py http://192.168.123.18:5052` |
| `lab_box_pick_cycle.py` | `python scripts/lab/lab_box_pick_cycle.py --base http://192.168.123.18:5052` |

Aggiungere qui nuovi script lab; non inserirli in `REMOTE_PUSH_FILES` senza review.

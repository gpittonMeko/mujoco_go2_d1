# Demo H2 — Documentazione

**Apri `APRI_DOCUMENTAZIONE.html` nel browser** (doppio click).  
I file `.md` in `markdown/` sono solo sorgente per aggiornamenti.

---

## Cosa leggere

| Documento | Per chi |
|-----------|---------|
| **01_LA_DEMO.md** | Tutti — cosa fa la demo e come lanciarla |
| **02_ACCENSIONE_ACCESSI_MANI.md** | Operatori — accensione robot, password, mani Revo |
| **03_FILE_SUL_ROBOT.md** | Dove sta il codice sul robot |

---

## In sintesi

**Robot:** Unitree H2  
**Demo gira su:** Jetson Thor `192.168.123.163`  
**Mani Revo:** servizio sul PC `192.168.123.162` (va acceso prima della demo)

```powershell
python scripts/h2_start_brainco_pc2.py          # mani ON
python scripts/h2_smoke_remote.py demo --confirm-arm   # demo
```

Repository PC lab: `mujoco_go2_d1`

# La demo — cosa fa e come lanciarla

## Cosa succede (circa 30 secondi)

1. **Voce TTS** — «Pellegrino Casoria di Accenture…»
2. **In parallelo** (~16 s):
   - parte l’audio Cyberpunk
   - il **braccio sinistro** sale lentamente
   - la **mano sinistra** (BrainCo Revo) si chiude
3. Braccio e mano tornano alla posizione di riposo

Tutto parte dalla **Jetson Thor** (`.163`) via rete DDS.  
Le mani funzionano solo se il servizio sul **PC `.162`** è attivo.

---

## Prima di lanciare — checklist

- [ ] Robot sul **protection frame**, in piedi, **piedi a terra**
- [ ] Telecomando: **Regular Mode 1** (non Mode 2 — vedi sotto)
- [ ] Area libera intorno al braccio sinistro
- [ ] Cavo Ethernet PC lab ↔ rete `192.168.123.x`
- [ ] Servizio mani Revo attivo sul `.162`
- [ ] Operatore presente con telecomando

---

## Accensione robot (telecomando)

```
Zero Torque  →  Damping  →  Locked Standing
       ↓
   ATTENDI piedi a terra (robot non sollevato)
       ↓
   Regular Mode 1   ← usare questo per la demo
```

| Modalità | Gesti braccio dal telecomando (kiss, clap…) | Per la demo |
|----------|-----------------------------------------------|-------------|
| **Regular Mode 1** | No | **Sì — usare questa** |
| Regular Mode 2 | Sì | No — interferisce col braccio automatico |

---

## Comandi (dal PC lab)

Apri PowerShell nella cartella `mujoco_go2_d1`:

```powershell
# 1) Attiva mani Revo (dopo ogni reboot del .162)
python scripts/h2_start_brainco_pc2.py

# 2) Controlli rapidi
python scripts/h2_smoke_remote.py recover-check
python scripts/h2_smoke_remote.py hand-dds

# 3) Demo
python scripts/h2_smoke_remote.py demo --confirm-arm
```

`--confirm-arm` = confermi che il robot è in piedi e l’area è libera.

---

## Se qualcosa non va

| Problema | Soluzione |
|----------|-----------|
| Mani non rispondono | Riavvia servizio sul `.162` (vedi doc 02) o `h2_start_brainco_pc2.py` |
| Braccio rifiutato | Robot in Damp? Ripristina da telecomando, poi Regular Mode 1 |
| Thor non raggiungibile | Verifica cavo e IP `192.168.123.163` |

---

## Sicurezza (3 regole)

1. **Non** usare Damp automatico da codice con il protection frame montato.
2. Demo solo con operatore presente e telecomando a portata.
3. Durante la demo: **Regular Mode 1**, nessuno preme gesti braccio sul controller.

# Accensione, accessi e mani Revo

## Macchine in rete

| IP | Cosa è | Accesso |
|----|--------|---------|
| `.161` | PC locomozione Unitree | **Non accessibile** |
| **`.162`** | PC mani BrainCo Revo | SSH solo per **accendere le mani** |
| **`.163`** | Jetson Thor — **qui gira la demo** | SSH libero (lab) |
| `.50` (es.) | PC laboratorio | Da qui lanci i comandi |

---

## Password

| Macchina | Utente | Password | Quando serve |
|----------|--------|----------|--------------|
| **Thor `.163`** | `unitree` | `123` | Debug, esecuzione manuale demo |
| **PC2 `.162`** | `unitree` | `Unitree#24226` | **Solo per attivare le mani Revo** |

> Sul `.162` non serve fare altro: niente installazioni, niente modifiche. Solo avviare il servizio mani.

---

## Mani Revo — come attivarle

Dopo ogni **spegnimento o reboot del `.162`**, le mani non funzionano finché non riavvii il servizio.

### Metodo A — dal PC lab (consigliato)

```powershell
python scripts/h2_start_brainco_pc2.py
```

Verifica:

```powershell
python scripts/h2_smoke_remote.py hand-dds
```

Deve comparire `OK` su `rt/brainco/left/state`.

### Metodo B — SSH diretto sul `.162`

Solo per accendere le mani:

```bash
ssh unitree@192.168.123.162
# password: Unitree#24226

cd ~/brainco_hand_service/bin
sudo ./brainco_hand_server -n eth0
```

Log errori: `/tmp/brainco_hand_server.log`

---

## Jetson Thor — accesso opzionale

```bash
ssh unitree@192.168.123.163
# password: 123

cd ~/h2_demo
export CYCLONEDDS_HOME=/usr/local LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH H2_DDS_IFACE=eth10
python3 scripts/h2_demo_casoria.py --iface eth10 --yes
```

Di solito basta lanciare la demo dal PC lab con `h2_smoke_remote.py`.

---

## Come funziona il collegamento mani

```
Mani Revo (RS-485)  →  PC .162  →  brainco_hand_server
                                        ↓ DDS
                              Thor .163  →  comanda mano sinistra nella demo
```

La Thor **non** ha la seriale delle mani: senza `.162` attivo la demo fa audio e braccio ma **la mano resta ferma**.

---

## Mode locomozione `ai` (opzionale)

Se `recover-check` non mostra mode `ai`:

```powershell
python scripts/h2_smoke_remote.py recover-select-ai --confirm-recovery
```

Non sostituisce Regular Mode 1 sul telecomando: sono due cose diverse.

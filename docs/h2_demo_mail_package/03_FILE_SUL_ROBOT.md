# Dove sono i file

## Jetson Thor — `192.168.123.163`

Tutto il demo:

```text
/home/unitree/h2_demo/
├── scripts/h2_demo_casoria.py    ← demo principale
├── scripts/h2_common.py          ← braccio
├── scripts/h2_hand_util.py       ← mani
├── data/audio/
│   ├── pellegrino_tts.wav
│   └── cyberpunk_meme.wav
└── unitree_sdk2_python/          ← SDK Unitree
```

SSH: `unitree` / `123`

---

## PC2 mani — `192.168.123.162`

Solo servizio mani:

```text
/home/unitree/brainco_hand_service/bin/brainco_hand_server
```

SSH: `unitree` / `Unitree#24226` — **solo per avviare le mani**

---

## PC laboratorio

Repository con script di controllo:

```text
mujoco_go2_d1/
├── scripts/h2_smoke_remote.py         ← lancia la demo
├── scripts/h2_start_brainco_pc2.py    ← accende mani sul .162
└── scripts/deploy_h2_demo_to_jetson.py
```

---

## Stato attuale sul robot

| Cosa | Stato |
|------|--------|
| Demo installata su Thor | Sì (`~/h2_demo`) |
| Demo testata | Sì |
| Mani dopo reboot `.162` | **Riavviare servizio** |

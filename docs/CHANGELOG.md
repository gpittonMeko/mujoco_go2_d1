# Changelog

## 2025-03-11 (2)

- Integrato depth camera OpenCV nel simulatore Python (da lupinjia/unitree_mujoco)
- Aggiunto modulo `image_publisher` per pubblicazione depth su DDS
- Config depth camera in `config.py` (ENABLE_DEPTH_CAMERA, NEAR/FAR_CLIP)
- Clonato `go2_deploy` con modelli pre-addestrati (Teacher-Student + Walk-These-Ways)
- Aggiunto `scripts/deploy_policy.py` – deploy policy RL via DDS (--model ts|wtw)
- Aggiornata documentazione

## 2025-03-11

- Creazione cartella `docs/`
- Aggiunto `scripts/test_movimento.py` – test stand up/squat/stand down
- Aggiunto `scripts/trot_gait.py` – policy open source trot gait
- Integrata camera depth in `go2.xml` (da fork lupinjia/unitree_mujoco)
- Documentazione consolidata in `docs/README.md`

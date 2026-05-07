# Virtual Joystick

Joystick virtuale per il controllo del movimento del Go2 in simulazione.

**Controlli tastiera:**
- **W / S** – avanti / indietro (vx)
- **A / D** – sinistra / destra (vy)
- **Q / E** – ruota sinistra / destra (vyaw)
- **Spazio** – stop (azzera velocità)
- **Esc** – chiudi

## Uso

```bash
# Terminale 1: avvia il simulatore
cd unitree_mujoco/simulate_python && python3 unitree_mujoco.py

# Terminale 2: avvia il joystick virtuale
python3 virtual_joystick/main.py
```

Opzionale: `--model wtw` per usare la policy Walk-These-Ways invece di Teacher-Student.

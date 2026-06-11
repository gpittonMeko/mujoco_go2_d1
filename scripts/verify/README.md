# Verify HTTP (lab)

Usa **`verify_go2_lab.py`** come entry point unico:

```bash
python scripts/verify_go2_lab.py dashboard http://192.168.123.18:5052
python scripts/verify_go2_lab.py quick http://192.168.123.18:5052
python scripts/verify_go2_lab.py nx http://192.168.123.18:5052
python scripts/verify_go2_lab.py worker http://192.168.123.3:8765
```

I file `verify_*.py` singoli restano per compatibilità e per import da `verify_go2_lab.py`; non aggiungerne di nuovi — estendere i subcomandi.

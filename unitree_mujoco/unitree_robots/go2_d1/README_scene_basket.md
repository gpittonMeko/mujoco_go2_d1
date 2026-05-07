# scene_basket.xml — scena alternativa con cesta e 5 palle

Versione alternativa della scena con:
- **Cesta cava** (diametro raddoppiato, r=0.28 m) al posto del tavolo
- **5 palle rosse** sopra la cesta che cadono dentro

## Uso

Per usare questa scena, modifica `config.py`:

```python
ROBOT_SCENE = "../unitree_robots/go2_d1/scene_basket.xml"
```

Oppure rinomina temporaneamente:
```bash
mv scene.xml scene_table.xml
mv scene_basket.xml scene.xml
```

**Nota:** Con `scene_basket.xml` serve la versione multi-palla di `unitree_mujoco.py` (supporto `red_ball_1` … `red_ball_5`).

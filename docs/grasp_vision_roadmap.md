# Roadmap visione grasp (oggetti generali)

Stato attuale (repo): AprilTag 0–3 + opzionale detector 2D (`scripts/box_object_detector.py`, Ultralytics/TensorRT o fallback contour) dentro `plan_from_frame` (`scripts/box_grasp_planner.py`). Target 3D senza tag usa euristica monocular da bbox.

## Soluzione 1 (operativa su NX)

- Flag sessione dashboard `GET/POST /api/arm/grasp_session` (priorità su env): fiducia IK polso, fused IK, fallback frontale, preferenza grip solo tag, execute arm.
- Trigger IK visibili in `grasp_pipeline_status` → `grasp_trigger_params` (diagonale px, loss debounce).

## Soluzione 2 (dopo — modelli)

Indicazioni letteratura / stack real-time (2025–2026):

1. **YOLO + keypoint → 6D** — [Yolo-Key-6D](https://arxiv.org/abs/2603.03879): una rete, testa keypoint, rotazione continua; adatto a TensorRT su Jetson se restate su Ultralytics.
2. **Categoria / articolati** — [YOEO](http://arxiv.org/abs/2506.05719): istanze + NPCS per pose category-level.
3. **Zero-shot / foundation** — pipeline tipo detector + pose tracker (es. [2604.17258](https://arxiv.org/abs/2604.17258)) per classi aperte.
4. **2D + depth / point cloud** — fusione con RealSense o LiDAR per pile / occlusioni ([esempio MDPI 2025](https://www.mdpi.com/2076-3417/15/12/6583)).

Integrazione prevista: nuovo modulo o estensione output verso `plan_from_frame` (target 6D + covarianza opzionale), calibrazione hand–eye, filtro temporale prima dell’IK template D1.

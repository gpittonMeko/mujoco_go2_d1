# Presa 6D D1 sulla Jetson NX

La presa usa la D456 al polso, il solver SE(3) locale e l'unico daemon hold già presente.
Non avvia publisher DDS aggiuntivi e non esegue mai release automaticamente.

## Sequenza di attivazione

1. Verificare RGB-D senza muovere il robot:
   `curl -X POST http://127.0.0.1:5056/api/pick/rgbd/health`
2. Fissare un marker ArUco `DICT_4X4_50`, ID `0`, lato 60 mm; in alternativa usare una scacchiera 7×5 angoli interni con quadretti da 25 mm. Portare manualmente il braccio in almeno 8 pose diverse e, mantenendolo in HOLD, chiamare per ogni posa:
   `curl -X POST http://127.0.0.1:5056/api/pick/metric/calibration/sample`
3. Costruire la hand-eye calibration:
   `curl -X POST http://127.0.0.1:5056/api/pick/metric/calibration/build`
4. Controllare point cloud, cuboide, candidati e IK senza movimento:
   `curl -X POST http://127.0.0.1:5056/api/pick/metric/preview`
5. Provare solo il pregrasp:
   `curl -H "Content-Type: application/json" -d '{"pregrasp_only":true}' http://127.0.0.1:5056/api/pick/grasp/goto`
6. Eseguire la presa completa soltanto dopo aver controllato il pregrasp:
   `curl -H "Content-Type: application/json" -d '{"confirm":"EXECUTE_GRASP6D"}' http://127.0.0.1:5056/api/pick/grasp/goto`

Per azzerare solo i campioni di calibrazione: `curl -X DELETE http://127.0.0.1:5056/api/pick/metric/calibration`.
La presa viene rifiutata se mancano depth, calibrazione, IK, stabilità su tre frame o clearance.

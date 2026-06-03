# Vision Workspace

Workspace per telecamere esterne sulla Jetson NX (separato da **D1 550 Workspace**).

## Camera target

- **Intel RealSense** (USB `8086:0b3a`) — stream RGB via V4L2 `/dev/videoN`
- Non usa la camera integrata Go2 / Orbbec polso (logico `0`)

## Dashboard

La visione è integrata nella **dashboard D1** (porta **5053**), non su una porta separata.

| Pagina | URL |
|--------|-----|
| Teach | http://192.168.123.18:5053/ |
| Programma | http://192.168.123.18:5053/program |
| **Vision** | http://192.168.123.18:5053/vision |

Deploy e avvio: stessi script del jog D1 (`deploy_d1_jog_to_nx.py`, `nx_start_d1_jog.sh`).

La vecchia dashboard standalone sulla porta 5054 non va più avviata.

#!/bin/bash
# Probe rapido su Jetson: rete LAN robot, USB, video, processo dashboard.
# Uso: bash scripts/nx_peripheral_probe.sh   (cwd = go2_visual_dashboard o path assoluto)
set +e
echo "=== host / time ==="
hostname
date -Is 2>/dev/null || date
echo ""
echo "=== IPs (brief) ==="
ip -brief addr 2>/dev/null || ifconfig -a 2>/dev/null | head -n 40
echo ""
echo "=== Ping candidati 192.168.123.x (1s, 1 pkt) ==="
for h in 192.168.123.1 192.168.123.18 192.168.123.20 192.168.123.161 192.168.123.222 192.168.123.100; do
  if ping -c 1 -W 1 "$h" >/dev/null 2>&1; then echo "OK   $h"; else echo "FAIL $h"; fi
done
echo ""
echo "=== UDP 2368 (XT-16) quick listen 0.35s ==="
python3 - <<'PY' 2>/dev/null || echo "(skip python udp)"
import select, socket, time
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
try:
    s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    s.bind(("0.0.0.0",2368))
    s.setblocking(False)
    t0=time.time(); n=0
    while time.time()-t0<0.35:
        r,_,_=select.select([s],[],[],0.08)
        if r:
            d,_=s.recvfrom(2048)
            if len(d)==568 and d[:4]==b"\xee\xff\x06\x01":
                n+=1
    print("xt16_like_packets", n)
finally:
    s.close()
PY
echo ""
echo "=== USB ==="
lsusb 2>/dev/null | head -n 40 || true
echo ""
echo "=== Video devices ==="
ls -l /dev/video* 2>/dev/null || echo "(no /dev/video*)"
echo ""
echo "=== serial (lidar/arm dongles) ==="
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || echo "(no ttyUSB/ttyACM)"
echo ""
echo "=== Dashboard HTTP (localhost) ==="
curl -sS -m 3 http://127.0.0.1:5050/api/health 2>/dev/null || echo "curl health failed"
echo ""
echo "=== serve_dashboard / diagnostics PIDs ==="
pgrep -af 'serve_dashboard_modular|diagnostics_dashboard' 2>/dev/null || echo "(none)"

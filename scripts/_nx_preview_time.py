import json
import paramiko
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.123.18", username="unitree", password="123", timeout=12)
cmd = (
    'curl -s -m 45 -w "\\nHTTP_TIME:%{time_total}" -X POST '
    'http://127.0.0.1:5052/api/grasp_coach/preview '
    '-H "Content-Type: application/json" '
    '-d \'{"instruction":"test"}\''
)
_i, o, e = c.exec_command(cmd, timeout=60)
raw = o.read().decode(errors="replace")
if "HTTP_TIME:" in raw:
    body, tline = raw.rsplit("HTTP_TIME:", 1)
    print("time_s", tline.strip())
    try:
        j = json.loads(body)
        print("ok", j.get("ok"), "reason", j.get("reason"))
        mg = j.get("metric_grounding") or {}
        print("mg_ok", mg.get("ok"), "mg_reason", mg.get("reason"))
        print("viz", j.get("metric_viz_url"))
    except Exception as exc:
        print("parse", exc)
        print(body[:800])
else:
    print(raw[:1500])
c.close()

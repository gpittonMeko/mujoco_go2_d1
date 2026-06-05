import paramiko, sys, time, hashlib, subprocess
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def connect():
    for a in range(1, 14):
        try:
            cc = paramiko.SSHClient()
            cc.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cc.connect("192.168.123.18", username="unitree", password="123",
                       timeout=10, banner_timeout=12, auth_timeout=12)
            print(f"[ssh] connected attempt {a}")
            return cc
        except Exception as ex:
            print(f"[ssh] attempt {a} failed: {ex}")
            time.sleep(3)
    return None

c = connect()
if c is None:
    print("SSH_UNREACHABLE"); sys.exit(2)
BASE = "/home/unitree/go2_visual_dashboard"
sftp = c.open_sftp()
pairs = [
    (f"{BASE}/go2_dashboard/blueprints/grasp.py", "go2_dashboard/blueprints/grasp.py", "scripts/_nx_grasp.py"),
    (f"{BASE}/go2_dashboard/blueprints/operator_api/routes.py", "go2_dashboard/blueprints/operator_api/routes.py", "scripts/_nx_routes.py"),
]
for remote, local, tmp in pairs:
    try:
        sftp.get(remote, tmp)
        nx = open(tmp, "rb").read()
        loc = open(local, "rb").read()
        same = (nx.replace(b"\r\n", b"\n") == loc.replace(b"\r\n", b"\n"))
        print(f"{local}: nx_lines={nx.count(chr(10).encode())} loc_lines={loc.count(chr(10).encode())} IDENTICAL={same}")
    except Exception as ex:
        print(f"FAILED {remote}: {ex}")
sftp.close()
c.close()

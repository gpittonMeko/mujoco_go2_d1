#!/usr/bin/env python3
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from deploy_d1_jog_to_nx import REMOTE_BASE, nx_host, nx_password, nx_user
import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(nx_host(), username=nx_user(), password=nx_password(), timeout=25)
ssh.exec_command(
    "pkill -f serve_d1_jog_dashboard.py 2>/dev/null; "
    "pkill -f nx_d1_jog_supervise 2>/dev/null; "
    "pkill -f realsense 2>/dev/null; "
    "for v in /dev/video*; do fuser -k \"$v\" 2>/dev/null; done; "
    "sleep 3",
    timeout=25,
)
sftp = ssh.open_sftp()
for name in ("_pyrs_rgb_test_inline.py", "_pyrs_rgb_test_nx.sh"):
    local = ROOT / "scripts" / name
    remote = f"{REMOTE_BASE}/scripts/{name}"
    sftp.put(str(local), remote)
sftp.close()
_, o, e = ssh.exec_command(f"chmod +x {REMOTE_BASE}/scripts/_pyrs_rgb_test_nx.sh && bash {REMOTE_BASE}/scripts/_pyrs_rgb_test_nx.sh", timeout=120)
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err.strip():
    print("stderr:", err[-600:])
ssh.close()

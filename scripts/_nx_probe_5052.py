"""Test funzionale del lock Orbbec sulla NX (Linux, fcntl attivo)."""
import os
import paramiko

HOST = (os.environ.get("GO2_NX_HOST") or "192.168.123.18").strip()
USER = (os.environ.get("GO2_NX_USER") or "unitree").strip()
PWD = os.environ.get("GO2_NX_PASSWORD") or "123"

REMOTE_TEST = r'''
import sys, time
sys.path.insert(0, "/home/unitree/go2_visual_dashboard")
from go2_dashboard import orbbec_lock as L
print("enabled:", L.enabled(), "| holder iniziale:", L.holder_info())
# Prelazione come fa una presa: chiedi e attendi che lo stream 5052 ceda l'Orbbec.
t0 = time.time()
with L.orbbec_guard("grasp_capture_TEST", preempt=True, timeout_s=15) as st:
    dt = time.time() - t0
    print("preempt -> acquisito:", st.acquired, "| attesa s:", round(dt, 2), "| holder ora:", L.holder_info())
    # tieni 1.5s simulando la cattura
    time.sleep(1.5)
print("dopo rilascio, holder:", L.holder_info(), "| preempt_requested:", L.preempt_requested())
'''


def main() -> None:
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=10)
    # scrivi il test e lancialo con lo stesso python della dashboard
    sftp = cli.open_sftp()
    with sftp.open("/tmp/_orbbec_lock_test.py", "w") as f:
        f.write(REMOTE_TEST)
    sftp.close()
    _, out, err = cli.exec_command("cd /home/unitree/go2_visual_dashboard && python3 /tmp/_orbbec_lock_test.py", timeout=40)
    print(out.read().decode("utf-8", "replace"))
    e = err.read().decode("utf-8", "replace").strip()
    if e:
        print("[stderr]\n" + e)
    cli.exec_command("rm -f /tmp/_orbbec_lock_test.py")
    cli.close()


if __name__ == "__main__":
    main()

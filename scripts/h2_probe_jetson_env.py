#!/usr/bin/env python3
"""One-off probe of Jetson Python/DDS environment (run from PC)."""
import paramiko

HOST, USER, PW = "192.168.123.163", "unitree", "123"
cmds = [
    "python3 --version",
    "which cmake",
    "python3 -c 'import numpy; print(\"numpy\", numpy.__version__)'",
    "python3 -c 'import setuptools; print(\"setuptools\", setuptools.__version__)'",
    "python3 -c 'import cyclonedds; print(\"cyclonedds\", cyclonedds.__version__)'",
    "python3 -c 'import unitree_sdk2py; print(\"unitree\", unitree_sdk2py.__file__)'",
    "ls -la /usr/local/lib/libddsc.so",
    "find /usr/local -name '*cyclonedds*' 2>/dev/null | head -20",
    "find /home/unitree -name 'cyclonedds' -type d 2>/dev/null | head -10",
    "find /usr -path '*site-packages/cyclonedds*' 2>/dev/null | head -10",
    "python3 -m site",
    "find /home/unitree -name 'crc_aarch64.so' 2>/dev/null | head -5",
    "ls -la /home/unitree/h2_demo/unitree_sdk2_python/unitree_sdk2py/utils/lib 2>/dev/null || echo no_lib_dir",
]

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PW, timeout=15, allow_agent=False, look_for_keys=False)
for cmd in cmds:
    print(f"=== {cmd}")
    _, o, e = c.exec_command(cmd, timeout=20)
    out = o.read().decode("utf-8", errors="replace").strip()
    err = e.read().decode("utf-8", errors="replace").strip()
    if out:
        print(out)
    if err:
        print("ERR:", err)
c.close()

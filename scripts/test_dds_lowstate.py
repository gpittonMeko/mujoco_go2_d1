#!/usr/bin/env python3
"""
Test rapido: riceve un messaggio LowState dal simulatore.
Se funziona, DDS è ok. Se resta in attesa, problema di interfaccia/multicast.
"""
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_sdk2_python"))

from unitree_sdk2py.core.channel import ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_

def main():
    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "unitree_mujoco", "simulate_python"))
        import config as sim_config
        iface = sim_config.INTERFACE
    except ImportError:
        iface = "lo"

    print(f"Test DDS LowState (domain=1, interface={iface})")
    print("Avvia PRIMA il simulatore in un altro terminale.")
    print("Attendo primo messaggio (timeout 15s)...")
    ChannelFactoryInitialize(1, iface)

    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(lambda msg: None, 10)

    msg = sub.Read(timeout=15.0)
    if msg:
        print("OK: LowState ricevuto! DDS funziona.")
        print(f"  motor_state[0].q = {msg.motor_state[0].q}")
    else:
        print("ERRORE: nessun messaggio. Prova:")
        print("  1. sudo ip link set lo multicast on")
        print("  2. --interface lan2 se lo non funziona")
        sys.exit(1)

if __name__ == "__main__":
    main()

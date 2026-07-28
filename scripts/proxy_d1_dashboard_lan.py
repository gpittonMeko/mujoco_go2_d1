#!/usr/bin/env python3
"""Espone la dashboard D1 5056 del cane anche sulla rete del PC lab.

Il cane e' tipicamente solo su 192.168.123.x. Il collega sul Wi‑Fi ufficio
non lo raggiunge. Questo proxy, lanciato sul PC gia' in rete cane, ascolta
su 0.0.0.0:PORT e inoltra a NX:5056.

Uso (PC lab, PowerShell):
  python scripts/proxy_d1_dashboard_lan.py

Poi il collega apre (esempi):
  http://<IP-WiFi-del-PC-lab>:5056/
  http://<IP-ZeroTier-del-PC-lab>:5056/
"""
from __future__ import annotations

import argparse
import select
import socket
import socketserver
import sys
import threading


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            src.shutdown(socket.SHUT_RD)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class _ForwardHandler(socketserver.BaseRequestHandler):
    upstream_host: str = "192.168.123.18"
    upstream_port: int = 5056

    def handle(self) -> None:
        upstream = socket.create_connection((self.upstream_host, self.upstream_port), timeout=10)
        t1 = threading.Thread(target=_pipe, args=(self.request, upstream), daemon=True)
        t2 = threading.Thread(target=_pipe, args=(upstream, self.request), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        try:
            upstream.close()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Proxy LAN → dashboard D1 NX :5056")
    parser.add_argument("--listen", default="0.0.0.0", help="Bind address (default 0.0.0.0 = tutta la rete PC)")
    parser.add_argument("--port", type=int, default=5056, help="Porta locale esposta al collega")
    parser.add_argument("--upstream-host", default="192.168.123.18")
    parser.add_argument("--upstream-port", type=int, default=5056)
    args = parser.parse_args()

    class Handler(_ForwardHandler):
        upstream_host = args.upstream_host
        upstream_port = args.upstream_port

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer((args.listen, args.port), Handler)
    print(
        f"[d1-proxy] http://{args.listen}:{args.port}/ -> "
        f"http://{args.upstream_host}:{args.upstream_port}/",
        flush=True,
    )
    print("[d1-proxy] Leave this terminal open. Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[d1-proxy] stop", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

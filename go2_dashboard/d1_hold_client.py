"""Client for the process-independent D1 DDS hold daemon."""

from __future__ import annotations

import json
import os
import socket
from typing import Any


def external_hold_enabled() -> bool:
    return os.environ.get("D1_HOLD_DAEMON_EXTERNAL", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def socket_path() -> str:
    return (os.environ.get("D1_HOLD_SOCKET") or "/tmp/go2_d1_hold.sock").strip()


def request(payload: dict[str, Any], *, timeout_s: float = 1.5) -> dict[str, Any]:
    raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    chunks: list[bytes] = []
    try:
        endpoint = socket_path()
        if endpoint.startswith("tcp://"):
            host, port = endpoint[6:].rsplit(":", 1)
            sock_obj = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            address: Any = (host, int(port))
        else:
            sock_obj = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            address = endpoint
        with sock_obj as sock:
            sock.settimeout(timeout_s)
            sock.connect(address)
            sock.sendall(raw)
            while True:
                part = sock.recv(65536)
                if not part:
                    break
                chunks.append(part)
                if b"\n" in part:
                    break
    except (OSError, TimeoutError) as exc:
        return {
            "ok": False,
            "reason": "hold_daemon_unavailable",
            "detail": repr(exc),
            "socket": socket_path(),
        }
    try:
        return dict(json.loads(b"".join(chunks).splitlines()[0].decode("utf-8")))
    except (IndexError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {"ok": False, "reason": "hold_daemon_bad_response", "detail": repr(exc)}


def status(*, timeout_s: float = 0.5) -> dict[str, Any]:
    return request({"action": "status"}, timeout_s=timeout_s)


def publish(
    messages: list[dict[str, Any]], *, delay_ms: int = 0, timeout_s: float | None = None
) -> dict[str, Any]:
    delay = max(0, int(delay_ms))
    timeout = timeout_s if timeout_s is not None else max(3.0, len(messages) * delay / 1000.0 + 5.0)
    return request(
        {"action": "publish", "messages": messages, "delay_ms": delay},
        timeout_s=timeout,
    )

#!/usr/bin/env python3
"""Process-independent owner of the D1 DDS writer and pose heartbeat.

The dashboard is only a Unix-socket client. Restarting Flask therefore cannot
close the DDS writer or interrupt the funcode-2 hold heartbeat.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socketserver
import subprocess
import threading
import time
from typing import Any

try:
    import fcntl
except ImportError:  # Windows test host
    fcntl = None  # type: ignore[assignment]


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


class HoldController:
    def __init__(
        self,
        *,
        command_bin: str,
        domain: int,
        command_delay_ms: int,
        heartbeat_ms: int,
        state_path: str,
        fake_log: str | None = None,
    ) -> None:
        self.command_bin = command_bin
        self.domain = int(domain)
        self.command_delay_ms = int(command_delay_ms)
        self.heartbeat_s = max(0.02, int(heartbeat_ms) / 1000.0)
        self.state_path = Path(state_path)
        self.fake_log = Path(fake_log) if fake_log else None
        self.fake_publisher_alive = False
        self.lock = threading.RLock()
        self.proc: subprocess.Popen[str] | None = None
        self.publisher_generation = 0
        self.desired_coupled = False
        self.power_enabled = False
        self.last_pose: dict[str, Any] | None = None
        self.last_publish_mono = 0.0
        self.last_heartbeat_mono = 0.0
        self.heartbeat_count = 0
        self.publisher_restart_count = 0
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.started_mono = time.monotonic()
        self.stop_event = threading.Event()
        self._load_same_boot_state()
        self.thread = threading.Thread(target=self._heartbeat_loop, name="d1-hold-heartbeat", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2.0)
        with self.lock:
            proc = self.proc
            self.proc = None
            self.fake_publisher_alive = False
            if proc is not None and proc.poll() is None:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                    proc.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()

    def _load_same_boot_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if data.get("boot_id") != _boot_id():
            return
        pose = data.get("last_pose")
        self.last_pose = dict(pose) if isinstance(pose, dict) else None
        self.desired_coupled = bool(data.get("desired_coupled")) and self.last_pose is not None
        self.power_enabled = bool(data.get("power_enabled"))

    def _save_state_locked(self) -> None:
        payload = {
            "boot_id": _boot_id(),
            "desired_coupled": self.desired_coupled,
            "power_enabled": self.power_enabled,
            "last_pose": self.last_pose,
            "saved_at": time.time(),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _publisher_alive_locked(self) -> bool:
        if self.fake_log is not None:
            return self.fake_publisher_alive
        return self.proc is not None and self.proc.poll() is None

    def _spawn_locked(self) -> bool:
        if self.fake_log is not None:
            self.fake_publisher_alive = True
            self.publisher_generation += 1
            if self.publisher_generation > 1:
                self.publisher_restart_count += 1
            return True
        try:
            self.proc = subprocess.Popen(
                [self.command_bin, str(self.domain), str(self.command_delay_ms)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=os.environ.copy(),
            )
        except OSError as exc:
            self.proc = None
            self._record_error_locked(f"publisher_spawn_failed:{exc!r}")
            return False
        time.sleep(0.18)
        if self.proc.poll() is not None:
            self.proc = None
            self._record_error_locked("publisher_exited_during_start")
            return False
        self.publisher_generation += 1
        if self.publisher_generation > 1:
            self.publisher_restart_count += 1
        return True

    def _record_error_locked(self, message: str) -> None:
        self.last_error = message
        self.last_error_at = time.time()
        print(json.dumps({"level": "error", "event": message, "time": self.last_error_at}), flush=True)

    def _raw_write_locked(self, message: dict[str, Any], *, source: str) -> bool:
        if self.fake_log is not None:
            self.fake_log.parent.mkdir(parents=True, exist_ok=True)
            with self.fake_log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"source": source, "message": message}, separators=(",", ":")) + "\n")
        else:
            if self.proc is None or self.proc.poll() is not None or self.proc.stdin is None:
                return False
            try:
                self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                return False
        self.last_publish_mono = time.monotonic()
        return True

    def _restore_hold_locked(self) -> bool:
        if not self.desired_coupled or self.last_pose is None:
            return True
        seq = int(time.time() * 1000) % 100000
        restore: list[dict[str, Any]] = []
        if self.power_enabled:
            restore.append({"seq": seq, "address": 1, "funcode": 6, "data": {"power": 1}})
            seq += 1
        restore.append({"seq": seq, "address": 1, "funcode": 5, "data": {"mode": 1}})
        restore.append(dict(self.last_pose))
        return all(self._raw_write_locked(msg, source="restore") for msg in restore)

    def _ensure_publisher_locked(self) -> bool:
        if self._publisher_alive_locked():
            return True
        if not self._spawn_locked():
            return False
        return self._restore_hold_locked()

    def publish(self, messages: list[dict[str, Any]], *, delay_ms: int = 0) -> dict[str, Any]:
        if not messages:
            return {"ok": True, "count": 0, **self.status()}
        with self.lock:
            enables_couple = any(
                int(msg.get("funcode", -1)) == 5
                and isinstance(msg.get("data"), dict)
                and int(msg["data"].get("mode", 0)) == 1
                for msg in messages
            )
            supplies_pose = any(int(msg.get("funcode", -1)) == 2 for msg in messages)
            if enables_couple and self.last_pose is None and not supplies_pose:
                return {"ok": False, "reason": "couple_requires_pose", **self._status_locked()}
            if not self._ensure_publisher_locked():
                return {"ok": False, "reason": "publisher_start_failed", **self._status_locked()}
            sent = 0
            per_message_delay_s = min(0.5, max(0, int(delay_ms)) / 1000.0)
            for index, message in enumerate(messages):
                msg = dict(message)
                if not self._raw_write_locked(msg, source="client"):
                    self.proc = None
                    self._record_error_locked("publisher_write_failed")
                    return {"ok": False, "reason": "publisher_write_failed", "count": sent, **self._status_locked()}
                sent += 1
                fc = int(msg.get("funcode", -1))
                data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
                if fc == 6 and int(data.get("power", 0)) == 1:
                    self.power_enabled = True
                elif fc == 5:
                    self.desired_coupled = int(data.get("mode", 0)) == 1
                    if not self.desired_coupled:
                        self.last_pose = None
                elif fc == 2:
                    self.last_pose = msg
                if per_message_delay_s > 0 and index + 1 < len(messages):
                    time.sleep(per_message_delay_s)
            self._save_state_locked()
            return {"ok": True, "count": sent, "stream": True, **self._status_locked()}

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(self.heartbeat_s):
            with self.lock:
                if not self.desired_coupled or self.last_pose is None:
                    continue
                if not self._ensure_publisher_locked():
                    continue
                if self._raw_write_locked(dict(self.last_pose), source="heartbeat"):
                    self.last_heartbeat_mono = time.monotonic()
                    self.heartbeat_count += 1

    def _status_locked(self) -> dict[str, Any]:
        now = time.monotonic()
        publisher_alive = self._publisher_alive_locked()
        heartbeat_age_ms = None
        if self.last_heartbeat_mono > 0:
            heartbeat_age_ms = round((now - self.last_heartbeat_mono) * 1000.0, 1)
        publish_age_ms = None
        if self.last_publish_mono > 0:
            publish_age_ms = round((now - self.last_publish_mono) * 1000.0, 1)
        freshness_ms = heartbeat_age_ms if heartbeat_age_ms is not None else publish_age_ms
        hold_active = bool(
            publisher_alive
            and self.desired_coupled
            and self.last_pose is not None
            and freshness_ms is not None
            and freshness_ms <= max(500.0, self.heartbeat_s * 3000.0)
        )
        return {
            "protocol_version": 1,
            "daemon_pid": os.getpid(),
            "publisher_pid": self.proc.pid if self.proc is not None and self.proc.poll() is None else None,
            "publisher_alive": publisher_alive,
            "publisher_generation": self.publisher_generation,
            "publisher_restart_count": self.publisher_restart_count,
            "desired_coupled": self.desired_coupled,
            "hold_active": hold_active,
            "has_pose": self.last_pose is not None,
            "last_publish_age_ms": publish_age_ms,
            "heartbeat_age_ms": heartbeat_age_ms,
            "heartbeat_count": self.heartbeat_count,
            "heartbeat_period_ms": round(self.heartbeat_s * 1000.0, 1),
            "uptime_s": round(now - self.started_mono, 3),
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
        }

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {"ok": True, **self._status_locked()}


class HoldRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        controller: HoldController = self.server.controller  # type: ignore[attr-defined]
        try:
            request = json.loads(self.rfile.readline(1024 * 1024).decode("utf-8"))
            action = request.get("action")
            if action == "status":
                response = controller.status()
            elif action == "publish":
                messages = request.get("messages")
                if not isinstance(messages, list) or not all(isinstance(x, dict) for x in messages):
                    response = {"ok": False, "reason": "invalid_messages"}
                else:
                    response = controller.publish(messages, delay_ms=int(request.get("delay_ms", 0)))
            else:
                response = {"ok": False, "reason": "invalid_action"}
        except (ValueError, UnicodeDecodeError) as exc:
            response = {"ok": False, "reason": "bad_request", "detail": repr(exc)}
        self.wfile.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))


_UnixStreamServerBase = getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)


class HoldUnixServer(socketserver.ThreadingMixIn, _UnixStreamServerBase):
    daemon_threads = True
    allow_reuse_address = True


class HoldTcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default=os.environ.get("D1_HOLD_SOCKET", "/tmp/go2_d1_hold.sock"))
    parser.add_argument("--state", default=os.environ.get("D1_HOLD_STATE", "/tmp/go2_d1_hold_state.json"))
    parser.add_argument("--lock", default=os.environ.get("D1_HOLD_LOCK", "/tmp/go2_d1_hold.lock"))
    parser.add_argument("--command-bin", default=os.environ.get("D1_SDK_COMMAND_BIN", "bin/d1_sdk_command"))
    parser.add_argument("--domain", type=int, default=int(os.environ.get("GO2_DDS_DOMAIN", "0")))
    parser.add_argument("--command-delay-ms", type=int, default=int(os.environ.get("D1_JOG_DAEMON_DELAY_MS", "0")))
    parser.add_argument("--heartbeat-ms", type=int, default=int(os.environ.get("D1_HOLD_HEARTBEAT_MS", "100")))
    parser.add_argument("--fake-log", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    lock_stream = open(args.lock, "a+", encoding="utf-8")
    if fcntl is not None:
        try:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("D1_HOLD_DAEMON_ALREADY_RUNNING", flush=True)
            lock_stream.close()
            return 73

    tcp_endpoint = args.socket.startswith("tcp://")
    socket_path: Path | None = None
    if tcp_endpoint:
        host, port = args.socket[6:].rsplit(":", 1)
        server_address: Any = (host, int(port))
    else:
        socket_path = Path(args.socket)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        server_address = str(socket_path)
    controller = HoldController(
        command_bin=args.command_bin,
        domain=args.domain,
        command_delay_ms=args.command_delay_ms,
        heartbeat_ms=args.heartbeat_ms,
        state_path=args.state,
        fake_log=args.fake_log,
    )
    server = (HoldTcpServer if tcp_endpoint else HoldUnixServer)(server_address, HoldRequestHandler)
    server.controller = controller  # type: ignore[attr-defined]
    if socket_path is not None:
        os.chmod(socket_path, 0o660)
    stopping = threading.Event()

    def stop_server(_signum: int, _frame: Any) -> None:
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    controller.start()
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        controller.close()
        if socket_path is not None:
            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass
        lock_stream.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

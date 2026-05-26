"""Lettura angoli servo D1 via subprocess (stessa logica operativa del monolite, modulo isolato)."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


def _subprocess_run_with_nx_env(
    exec_argv: list[str],
    *,
    cwd: str,
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    env_sh = Path(cwd) / "scripts" / "nx_dashboard_env.sh"
    if os.name != "nt" and env_sh.is_file():
        inner = " ".join(shlex.quote(a) for a in exec_argv)
        script = f"cd {shlex.quote(str(cwd))} && . {shlex.quote(str(env_sh))} && exec {inner}"
        return subprocess.run(
            ["bash", "-c", script],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    return subprocess.run(
        exec_argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=os.environ.copy(),
    )


def _parse_servo_stdout(stdout: str) -> list[float] | None:
    full = _parse_servo_stdout_full(stdout)
    return full[0]


def _parse_servo_stdout_full(stdout: str) -> tuple[list[float] | None, dict[str, Any]]:
    """Ultimo campione + statistiche su tutte le righe ``servo_angles`` (rileva cambio durante una lettura)."""
    samples: list[list[float]] = []
    for line in (stdout or "").splitlines():
        if not line.startswith("servo_angles "):
            continue
        parts = line.split()[1:]
        if len(parts) < 7:
            continue
        try:
            samples.append([float(v) for v in parts[:7]])
        except ValueError:
            continue
    if not samples:
        return None, {"servo_angle_lines": 0}
    latest = samples[-1]
    spread_per_j = [max(s[j] for s in samples) - min(s[j] for s in samples) for j in range(7)]
    stats: dict[str, Any] = {
        "servo_angle_lines": len(samples),
        "servo_max_spread_any_joint_deg": round(max(spread_per_j), 4),
        "servo_spread_per_joint_deg": [round(x, 4) for x in spread_per_j],
    }
    if len(samples) >= 2:
        diffs = [abs(samples[-1][j] - samples[0][j]) for j in range(7)]
        stats["servo_first_to_last_abs_delta_max_deg"] = round(max(diffs), 4)
        stats["servo_first_to_last_abs_delta_per_joint_deg"] = [round(x, 4) for x in diffs]
    return latest, stats


def read_servo_deg_with_diag(project_root: Path) -> tuple[list[float] | None, dict[str, Any]]:
    """Prima ``bin/d1_arm_feedback_helper``, poi ``scripts/d1_arm_servo_read_python.py``.

    L'argomento ``listen_s`` è un **timeout massimo**: gli helper escono appena arriva il primo
    campione servo (+ breve settle), così la dashboard non resta secondi senza pubblicare comandi DDS.
    """
    helper = project_root / "bin" / "d1_arm_feedback_helper"
    py_reader = project_root / "scripts" / "d1_arm_servo_read_python.py"
    listen_s = max(1, int(os.environ.get("D1_FEEDBACK_HELPER_LISTEN_S", "3")))
    timeout_s = float(os.environ.get("D1_FEEDBACK_HELPER_TIMEOUT_S", "14"))
    domain = int(os.environ.get("GO2_DDS_DOMAIN", "0"))
    cwd = str(project_root)
    base: dict[str, Any] = {
        "helper_path": str(helper),
        "helper_exists": helper.is_file(),
        "python_reader_path": str(py_reader),
        "dds_domain": domain,
        "listen_s": listen_s,
        "timeout_subprocess_s": timeout_s,
    }

    def _merge_stderr(d: dict[str, Any], stderr: str) -> None:
        st = (stderr or "").strip()
        if st:
            d["stderr_tail"] = st[-900:]

    def _run_cpp() -> tuple[list[float] | None, dict[str, Any]]:
        d: dict[str, Any] = dict(base)
        d["backend"] = "cpp_subprocess"
        if not helper.is_file():
            d["reason"] = "MISSING_BINARY"
            return None, d
        if not os.access(helper, os.X_OK):
            d["reason"] = "HELPER_NOT_EXECUTABLE"
            return None, d
        cmd = [str(helper), str(domain), str(listen_s)]
        d["argv"] = cmd
        try:
            result = _subprocess_run_with_nx_env(cmd, cwd=cwd, timeout_s=timeout_s)
            d["returncode"] = int(result.returncode)
            _merge_stderr(d, (result.stderr or "").strip())
            latest, st = _parse_servo_stdout_full(result.stdout or "")
            d.update(st)
            if latest is not None:
                d["reason"] = "OK"
                return latest, d
            d["reason"] = "NO_SERVO_ANGLES_LINE"
            st = (result.stdout or "").strip()
            d["stdout_tail"] = st[-900:] if st else None
            return None, d
        except subprocess.TimeoutExpired as exc:
            d["reason"] = "HELPER_TIMEOUT"
            if exc.stdout:
                d["stdout_tail"] = str(exc.stdout)[-500:]
            return None, d
        except Exception as exc:
            d["reason"] = "SUBPROCESS_FAILED"
            d["error"] = repr(exc)
            return None, d

    def _run_python() -> tuple[list[float] | None, dict[str, Any]]:
        d: dict[str, Any] = dict(base)
        d["backend"] = "python_subprocess"
        if not py_reader.is_file():
            d["reason"] = "MISSING_PYTHON_READER"
            return None, d
        cmd = [sys.executable, str(py_reader), str(domain), str(listen_s)]
        d["argv"] = cmd
        try:
            result = _subprocess_run_with_nx_env(cmd, cwd=cwd, timeout_s=timeout_s)
            d["returncode"] = int(result.returncode)
            _merge_stderr(d, (result.stderr or "").strip())
            latest, st = _parse_servo_stdout_full(result.stdout or "")
            d.update(st)
            if latest is not None:
                d["reason"] = "OK"
                return latest, d
            d["reason"] = "NO_SERVO_ANGLES_LINE_PYTHON"
            return None, d
        except Exception as exc:
            d["reason"] = "PYTHON_SUBPROCESS_FAILED"
            d["error"] = repr(exc)
            return None, d

    order_raw = (os.environ.get("D1_SERVO_FEEDBACK_BACKEND_ORDER") or "cpp,python").lower()
    order = [x.strip() for x in order_raw.split(",") if x.strip() in {"cpp", "python"}]
    if not order:
        order = ["cpp", "python"]
    last: dict[str, Any] = dict(base)
    last["backends_tried"] = list(order)
    for kind in order:
        if kind == "cpp":
            angles, diag = _run_cpp()
        else:
            angles, diag = _run_python()
        last.update(diag)
        if angles is not None:
            diag["backends_tried"] = list(order)
            return angles, diag
    last.setdefault("reason", "ALL_BACKENDS_FAILED")
    return None, last

"""Lettura angoli servo D1 — SDK ufficiale (``d1_sdk_feedback``) con fallback legacy."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from go2_dashboard.paths import (
    D1_ARM_FEEDBACK_BIN,
    D1_ARM_SERVO_READ_PY,
    D1_SDK_FEEDBACK_BIN,
    PROJECT_ROOT,
)
from go2_dashboard.sdk_backend import prefer_sdk_backend


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
    """Ultimo campione + statistiche su tutte le righe ``servo_angles``."""
    import re

    samples: list[list[float]] = []
    for line in (stdout or "").splitlines():
        if line.startswith("servo_angles "):
            parts = line.split()[1:]
            if len(parts) >= 7:
                try:
                    samples.append([float(v) for v in parts[:7]])
                except ValueError:
                    continue
        m = re.search(
            r"servo0_data:([-\d.]+).*servo1_data:([-\d.]+).*servo2_data:([-\d.]+).*"
            r"servo3_data:([-\d.]+).*servo4_data:([-\d.]+).*servo5_data:([-\d.]+).*servo6_data:([-\d.]+)",
            line,
        )
        if m:
            try:
                samples.append([float(m.group(i)) for i in range(1, 8)])
            except ValueError:
                pass
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


def _read_via_sdk(project_root: Path) -> tuple[list[float] | None, dict[str, Any]]:
    from go2_dashboard.d1_jog import service as jog_svc

    fb = jog_svc.read_servo_deg(fast=False)
    diag: dict[str, Any] = {
        "backend": "d1_sdk_feedback",
        "helper_path": str(D1_SDK_FEEDBACK_BIN),
        "dds_domain": int(os.environ.get("GO2_DDS_DOMAIN", "0")),
        "returncode": fb.get("returncode"),
        "reason": fb.get("reason", "OK" if fb.get("ok") else "no_servo_feedback"),
    }
    if fb.get("stderr_tail"):
        diag["stderr_tail"] = fb["stderr_tail"]
    angles = fb.get("servo_deg")
    if angles is not None:
        diag["reason"] = "OK"
        return [round(float(x), 3) for x in angles[:7]], diag
    return None, diag


def _read_via_legacy_cpp(project_root: Path) -> tuple[list[float] | None, dict[str, Any]]:
    helper = D1_ARM_FEEDBACK_BIN
    listen_s = max(1, int(os.environ.get("D1_FEEDBACK_HELPER_LISTEN_S", "3")))
    timeout_s = float(os.environ.get("D1_FEEDBACK_HELPER_TIMEOUT_S", "14"))
    domain = int(os.environ.get("GO2_DDS_DOMAIN", "0"))
    cwd = str(project_root)
    base: dict[str, Any] = {
        "backend": "cpp_subprocess",
        "helper_path": str(helper),
        "helper_exists": helper.is_file(),
        "dds_domain": domain,
        "listen_s": listen_s,
        "timeout_subprocess_s": timeout_s,
    }
    if not helper.is_file():
        base["reason"] = "MISSING_BINARY"
        return None, base
    if not os.access(helper, os.X_OK):
        base["reason"] = "HELPER_NOT_EXECUTABLE"
        return None, base
    cmd = [str(helper), str(domain), str(listen_s)]
    base["argv"] = cmd
    try:
        result = _subprocess_run_with_nx_env(cmd, cwd=cwd, timeout_s=timeout_s)
        base["returncode"] = int(result.returncode)
        st = (result.stderr or "").strip()
        if st:
            base["stderr_tail"] = st[-900:]
        latest, spread_stats = _parse_servo_stdout_full(result.stdout or "")
        base.update(spread_stats)
        if latest is not None:
            base["reason"] = "OK"
            return latest, base
        base["reason"] = "NO_SERVO_ANGLES_IN_STDOUT"
        return None, base
    except subprocess.TimeoutExpired:
        base["reason"] = "SUBPROCESS_TIMEOUT"
        return None, base
    except OSError as exc:
        base["reason"] = "SUBPROCESS_OSERROR"
        base["detail"] = repr(exc)
        return None, base


def _read_via_legacy_python(project_root: Path) -> tuple[list[float] | None, dict[str, Any]]:
    py_reader = D1_ARM_SERVO_READ_PY
    timeout_s = float(os.environ.get("D1_FEEDBACK_HELPER_TIMEOUT_S", "14"))
    cwd = str(project_root)
    base: dict[str, Any] = {
        "backend": "python_subprocess",
        "python_reader_path": str(py_reader),
        "timeout_subprocess_s": timeout_s,
    }
    if not py_reader.is_file():
        base["reason"] = "MISSING_PYTHON_READER"
        return None, base
    cmd = [sys.executable, str(py_reader)]
    base["argv"] = cmd
    try:
        result = _subprocess_run_with_nx_env(cmd, cwd=cwd, timeout_s=timeout_s)
        base["returncode"] = int(result.returncode)
        st = (result.stderr or "").strip()
        if st:
            base["stderr_tail"] = st[-900:]
        latest, spread_stats = _parse_servo_stdout_full(result.stdout or "")
        base.update(spread_stats)
        if latest is not None:
            base["reason"] = "OK"
            return latest, base
        base["reason"] = "NO_SERVO_ANGLES_IN_STDOUT"
        return None, base
    except subprocess.TimeoutExpired:
        base["reason"] = "SUBPROCESS_TIMEOUT"
        return None, base
    except OSError as exc:
        base["reason"] = "SUBPROCESS_OSERROR"
        base["detail"] = repr(exc)
        return None, base


def read_servo_deg_with_diag(project_root: Path) -> tuple[list[float] | None, dict[str, Any]]:
    """Feedback servo: ``d1_sdk_feedback`` se disponibile, altrimenti helper legacy."""
    root = PROJECT_ROOT if project_root is None else Path(project_root)
    if prefer_sdk_backend():
        try:
            return _read_via_sdk(root)
        except Exception as exc:
            diag = {"backend": "d1_sdk_feedback", "reason": "sdk_read_exception", "detail": repr(exc)}
            # fallback sotto
    cur, diag = _read_via_legacy_cpp(root)
    if cur is not None:
        return cur, diag
    return _read_via_legacy_python(root)

"""Lock advisory cross-process per l'unico Orbbec fisico (anti-conflitto camera).

Problema: più consumatori toccano lo **stesso** Orbbec (nodi V4L2 ``/dev/video0-3`` =
un solo device USB ``2bc5:080b``): la presa via SDK (``pyorbbecsdk`` → claim dell'intero
device), il probe RGB (``usb_auto_v4l_mapping``), lo stream ``CameraCache`` e, soprattutto,
**processi distinti** (dashboard :5052 vs jog :5053). Su Linux un solo processo per volta
può aprire il device: se due ci provano insieme → ``EBUSY`` / frame neri / pipeline che non
parte. È il classico "rubarsi la camera".

Soluzione: un ``flock`` (advisory, cross-process) su un file condiviso che **tutti** i
percorsi Orbbec acquisiscono prima di aprire il device. In più una **prelazione cooperativa**:
una presa on-demand può chiedere (``preempt=True``) che uno stream continuo ceda subito la
camera, così la presa ha priorità e non resta bloccata.

Note:
- Su Windows / senza ``fcntl`` il modulo è **no-op** (sviluppo PC: nessun lock, tutto "libero").
- Disattivabile con ``GO2_ORBBEC_LOCK=0`` (sconsigliato sulla NX condivisa).
- Stesso utente ``unitree`` sui due servizi → il file di lock di default in ``tempdir`` va bene;
  override con ``GO2_ORBBEC_LOCK_PATH``.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from typing import Iterator

try:  # solo Linux: flock advisory cross-process
    import fcntl
except Exception:  # pragma: no cover - Windows/dev
    fcntl = None  # type: ignore[assignment]


def _lock_path() -> str:
    p = (os.environ.get("GO2_ORBBEC_LOCK_PATH") or "").strip()
    if p:
        return p
    return os.path.join(tempfile.gettempdir(), "go2_orbbec.lock")


def _preempt_path() -> str:
    return _lock_path() + ".preempt"


def _holder_path() -> str:
    return _lock_path() + ".holder"


def enabled() -> bool:
    """True se il lock è operativo (Linux + ``GO2_ORBBEC_LOCK`` non disabilitato)."""
    if fcntl is None:
        return False
    flag = (os.environ.get("GO2_ORBBEC_LOCK", "1") or "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def _default_timeout_s() -> float:
    try:
        return float((os.environ.get("GO2_ORBBEC_LOCK_TIMEOUT_S") or "12").strip())
    except ValueError:
        return 12.0


def _preempt_grace_s() -> float:
    try:
        return float((os.environ.get("GO2_ORBBEC_PREEMPT_GRACE_S") or "8").strip())
    except ValueError:
        return 8.0


class LockState:
    """Esito di ``orbbec_guard``: ``acquired`` e (se occupato) ``holder`` descrittivo."""

    __slots__ = ("acquired", "holder")

    def __init__(self, acquired: bool, holder: str | None = None) -> None:
        self.acquired = bool(acquired)
        self.holder = holder


class OrbbecLease:
    """Handle a basso livello (per usi long-lived, es. stream): rilascia con ``release``."""

    __slots__ = ("_fd", "acquired", "purpose")

    def __init__(self, fd: int | None, acquired: bool, purpose: str) -> None:
        self._fd = fd
        self.acquired = bool(acquired)
        self.purpose = purpose


def holder_info() -> str | None:
    """Descrizione dell'attuale detentore del lock (best-effort, per diagnostica/UI)."""
    try:
        with open(_holder_path(), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _write_holder(purpose: str) -> None:
    try:
        with open(_holder_path(), "w", encoding="utf-8") as f:
            f.write(f"{purpose} pid={os.getpid()} ts={time.time():.0f}")
    except OSError:
        pass


def request_preempt(purpose: str = "") -> None:
    """Segnala agli stream continui di **cedere** subito l'Orbbec (prelazione cooperativa)."""
    if not enabled():
        return
    try:
        with open(_preempt_path(), "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "pid": os.getpid(), "purpose": purpose}))
    except OSError:
        pass


def clear_preempt() -> None:
    if not enabled():
        return
    try:
        os.unlink(_preempt_path())
    except OSError:
        pass


def preempt_requested(max_age_s: float | None = None) -> bool:
    """True se qualcuno ha chiesto la prelazione (file fresco): lo stream deve cedere."""
    if not enabled():
        return False
    if max_age_s is None:
        max_age_s = _preempt_grace_s()
    try:
        mtime = os.stat(_preempt_path()).st_mtime
    except OSError:
        return False
    return (time.time() - mtime) <= float(max_age_s)


def acquire(
    purpose: str,
    *,
    blocking: bool = False,
    timeout_s: float = 0.0,
    poll_s: float = 0.1,
) -> OrbbecLease | None:
    """Prova ad acquisire il lock Orbbec. Ritorna un ``OrbbecLease`` o ``None`` se occupato.

    Con ``blocking=True`` attende fino a ``timeout_s``. Su sistemi senza lock (Windows/dev o
    ``GO2_ORBBEC_LOCK=0``) ritorna sempre un lease no-op "acquisito".
    """
    if not enabled():
        return OrbbecLease(fd=None, acquired=True, purpose=purpose)
    try:
        fd = os.open(_lock_path(), os.O_CREAT | os.O_RDWR, 0o666)
    except OSError:
        return OrbbecLease(fd=None, acquired=True, purpose=purpose)  # fail-open: non bloccare la camera
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _write_holder(purpose)
            return OrbbecLease(fd=fd, acquired=True, purpose=purpose)
        except OSError:
            if not blocking or time.monotonic() >= deadline:
                try:
                    os.close(fd)
                except OSError:
                    pass
                return None
            time.sleep(max(0.02, float(poll_s)))


def release(lease: OrbbecLease | None) -> None:
    if lease is None:
        return
    fd = lease._fd
    lease.acquired = False
    lease._fd = None
    if fd is None:
        return
    try:
        try:
            os.unlink(_holder_path())
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


@contextmanager
def orbbec_guard(
    purpose: str,
    *,
    blocking: bool = True,
    timeout_s: float | None = None,
    poll_s: float = 0.1,
    preempt: bool = False,
) -> Iterator[LockState]:
    """Context manager: serializza l'accesso all'Orbbec.

    ``with orbbec_guard("grasp_capture", preempt=True) as st:`` — se ``st.acquired`` è False la
    camera è occupata (``st.holder`` dice da chi). Con ``preempt=True`` chiede agli stream
    continui di cedere la camera per tutta la durata del blocco.
    """
    if timeout_s is None:
        timeout_s = _default_timeout_s()
    asked = False
    lease: OrbbecLease | None = None
    try:
        if enabled() and preempt:
            request_preempt(purpose)
            asked = True
        lease = acquire(purpose, blocking=blocking, timeout_s=float(timeout_s), poll_s=poll_s)
        if lease is not None:
            yield LockState(True, None)
        else:
            yield LockState(False, holder_info())
    finally:
        if asked:
            clear_preempt()
        if lease is not None:
            release(lease)

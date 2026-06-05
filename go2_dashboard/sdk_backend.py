"""Scelta backend braccio D1 (SDK ufficiale vs helper legacy), isolata da ``paths.py``.

Questo modulo è volutamente **autonomo**: definisce ``prefer_sdk_backend`` / ``sdk_binaries_ready``
senza importare da ``go2_dashboard.paths``. Motivo: ``paths.py`` può essere ripristinato a una
versione più vecchia (deploy di un altro ramo) priva di queste funzioni, mandando in ``ImportError``
l'intera dashboard. Importando da qui, il backend resta stabile a prescindere da ``paths.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

D1_SDK_COMMAND_BIN = PROJECT_ROOT / "bin" / "d1_sdk_command"
D1_SDK_FEEDBACK_BIN = PROJECT_ROOT / "bin" / "d1_sdk_feedback"


def sdk_binaries_ready() -> bool:
    """True se i binari SDK ufficiali (``d1_sdk_command``/``d1_sdk_feedback``) sono compilati ed eseguibili."""
    return (
        D1_SDK_COMMAND_BIN.is_file()
        and os.access(D1_SDK_COMMAND_BIN, os.X_OK)
        and D1_SDK_FEEDBACK_BIN.is_file()
        and os.access(D1_SDK_FEEDBACK_BIN, os.X_OK)
    )


def prefer_sdk_backend() -> bool:
    """Backend movimento/feedback come dashboard jog (d1_sdk_*), se compilati."""
    if os.environ.get("D1_USE_SDK_BACKEND", "1").lower() in {"0", "false", "no", "off"}:
        return False
    return sdk_binaries_ready()

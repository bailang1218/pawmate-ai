"""Application icon helpers."""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Any

from pawmate.qt_compat import QApplication, QIcon


_APP_USER_MODEL_ID = "PawMateAI.PawMate"
_APP_USER_MODEL_ID_SET = False


def get_app_icon_path() -> Path | None:
    """Return the bundled PawMate avatar image used as the app icon."""
    image_dir = Path(__file__).resolve().parent / "web" / "images"
    for filename in ("avatar_transparent.png", "avatar.png"):
        candidate = image_dir / filename
        if candidate.exists():
            return candidate
    return None


def apply_app_icon(window: Any | None = None) -> QIcon:
    """Apply the bundled PawMate avatar to QApplication and an optional window."""
    _set_windows_app_user_model_id()
    icon_path = get_app_icon_path()
    icon = QIcon(str(icon_path)) if icon_path is not None else QIcon()
    if icon.isNull():
        return icon

    app = QApplication.instance()
    if app is not None:
        app.setWindowIcon(icon)
    if window is not None and hasattr(window, "setWindowIcon"):
        window.setWindowIcon(icon)
    return icon


def _set_windows_app_user_model_id() -> None:
    """Give Windows a stable taskbar identity instead of grouping as python.exe."""
    global _APP_USER_MODEL_ID_SET
    if _APP_USER_MODEL_ID_SET or not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_APP_USER_MODEL_ID)
        _APP_USER_MODEL_ID_SET = True
    except Exception:
        pass

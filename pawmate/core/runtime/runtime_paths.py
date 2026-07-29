"""
Runtime paths — detect frozen/exe bundling and resolve resource paths.

Supports PyInstaller / cx_Freeze and similar packaging tools.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def is_frozen() -> bool:
    """Check if running in a frozen (PyInstaller / cx_Freeze) exe."""
    return getattr(sys, "frozen", False) or bool(getattr(sys, "_MEIPASS", None))


def get_app_root() -> Path:
    """Get the application root directory.

    In frozen mode, returns the directory containing the exe.
    In development mode, returns the project root (parent of pawmate/).
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # In dev mode, assume cwd is the project root
    return Path.cwd()


def get_bundled_playwright_browsers_path() -> Optional[str]:
    """Get path to bundled Playwright browsers for frozen exe.

    Checks:
      1. PAWMATE_PLAYWRIGHT_BROWSERS_PATH env var
      2. PLAYWRIGHT_BROWSERS_PATH env var
      3. bundled/ms-playwright/ next to exe (frozen mode)
      4. Project-root level ms-playwright/ (dev mode)

    Returns None if not found.
    """
    # 1. App-specific override
    for var in ("PAWMATE_PLAYWRIGHT_BROWSERS_PATH", "PLAYWRIGHT_BROWSERS_PATH"):
        val = os.environ.get(var)
        if val:
            resolved = Path(val)
            if resolved.is_dir():
                return str(resolved)

    # 2. Bundled alongside exe
    if is_frozen():
        bundled = get_app_root() / "bundled" / "ms-playwright"
        if bundled.is_dir():
            return str(bundled)

    # 3. Project root (dev mode)
    dev_browsers = get_app_root() / "ms-playwright"
    if dev_browsers.is_dir():
        return str(dev_browsers)

    return None


def ensure_playwright_browsers_env() -> None:
    """Set PLAYWRIGHT_BROWSERS_PATH if bundled browsers exist.

    Call early in startup to ensure Playwright uses bundled browsers.
    """
    bp = get_bundled_playwright_browsers_path()
    if bp and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = bp
        import logging

        logging.getLogger("pawmate").info(
            "[RuntimePaths] PLAYWRIGHT_BROWSERS_PATH=%s", bp,
        )

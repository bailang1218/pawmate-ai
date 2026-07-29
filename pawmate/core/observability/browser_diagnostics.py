"""Browser dependency diagnostics for Playwright + Chromium.

Startup uses a filesystem-only quick check. Deep checks that spin up
Playwright's driver are reserved for real browser use or explicit diagnostics.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pawmate.core.safety.resource_limits import (
    MAX_COMMAND_OUTPUT_BYTES,
    read_binary_fileobj_limited,
)

_logger = logging.getLogger("pawmate")
_STATUS_CACHE: dict[tuple[bool, str, str], "BrowserDependencyStatus"] = {}


@dataclass
class BrowserDependencyStatus:
    """Result of a browser dependency check."""

    playwright_installed: bool
    chromium_installed: bool
    available: bool
    message: str
    install_hint: str
    python_executable: str = ""
    chromium_executable_path: str = ""
    browsers_path: str = ""
    playwright_version: str = ""
    detail: Optional[str] = None


def _get_install_hint() -> str:
    py = sys.executable
    return f'"{py}" -m pip install playwright && "{py}" -m playwright install chromium'


def _get_browsers_path() -> Optional[str]:
    """Resolve the Playwright browsers storage directory."""
    for var in ("PAWMATE_PLAYWRIGHT_BROWSERS_PATH", "PLAYWRIGHT_BROWSERS_PATH"):
        val = os.environ.get(var)
        if val:
            return val
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        bp = Path(local_app_data) / "ms-playwright"
        if bp.is_dir():
            return str(bp)
    return None


def _is_frozen() -> bool:
    """Check if running in a PyInstaller / frozen exe environment."""
    return getattr(sys, "frozen", False) or bool(getattr(sys, "_MEIPASS", None))


def _playwright_version() -> str:
    try:
        import playwright

        version = getattr(playwright, "__version__", "")
        if version:
            return str(version)
    except Exception:
        raise
    try:
        from importlib.metadata import version

        return version("playwright")
    except Exception:
        return "unknown"


def _find_chromium_executable_in_browsers_path(browsers_path: str | None) -> str:
    """Fast filesystem-only lookup for Playwright Chromium."""
    if not browsers_path:
        return ""
    bp = Path(browsers_path)
    if not bp.is_dir():
        return ""

    for cd in reversed(sorted(bp.glob("chromium-*"))):
        for candidate in (
            "chrome-win64/chrome.exe",
            "chrome-win/chrome.exe",
            "chrome/chrome.exe",
            "chrome.exe",
        ):
            exe = cd / candidate
            if exe.exists():
                return str(exe)
    return ""


def _find_chromium_executable_via_api() -> Optional[str]:
    """Use Playwright's API to find the chromium executable path."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            chromium = p.chromium
            for attr in ("executable_path", "_executable_path"):
                val = getattr(chromium, attr, None)
                if val:
                    return str(val)
    except Exception as exc:
        _logger.debug("[BrowserDiagnostics] API path lookup failed: %s", exc)
    return None


def _check_chromium_via_cli(
    *,
    python_executable: str,
    browsers_path: str,
    playwright_version: str,
) -> BrowserDependencyStatus | None:
    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            result = subprocess.run(
                [python_executable, "-m", "playwright", "install", "--dry-run", "chromium"],
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=10,
                env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": browsers_path} if browsers_path else None,
            )
            stdout_b, stdout_truncated = read_binary_fileobj_limited(
                stdout_file,
                MAX_COMMAND_OUTPUT_BYTES,
            )
            stderr_b, stderr_truncated = read_binary_fileobj_limited(
                stderr_file,
                MAX_COMMAND_OUTPUT_BYTES,
            )
        if stdout_truncated or stderr_truncated:
            raise RuntimeError("playwright dry-run output exceeded size limit")
        stdout = stdout_b.decode("utf-8", errors="replace")
        if result.returncode == 0 and "chromium" in stdout.lower():
            return BrowserDependencyStatus(
                playwright_installed=True,
                chromium_installed=True,
                available=True,
                message="Browser automation dependencies are available.",
                install_hint="",
                python_executable=python_executable,
                chromium_executable_path="(via playwright CLI)",
                browsers_path=browsers_path,
                playwright_version=playwright_version,
            )
    except Exception as exc:
        _logger.debug("[BrowserDiagnostics] subprocess check failed: %s", exc)
    return None


def check_browser_dependencies(
    *,
    deep: bool = False,
    use_cache: bool = True,
) -> BrowserDependencyStatus:
    """Check whether Playwright + Chromium are available.

    The default quick mode never launches a browser and never starts the
    Playwright driver process. Pass ``deep=True`` when a browser operation is
    about to run and the quick filesystem check was insufficient.
    """
    python_executable = sys.executable
    browsers_path = _get_browsers_path() or ""
    cache_key = (bool(deep), python_executable, browsers_path)
    if use_cache and cache_key in _STATUS_CACHE:
        return _STATUS_CACHE[cache_key]

    started = time.perf_counter()

    def remember(status: BrowserDependencyStatus) -> BrowserDependencyStatus:
        if use_cache:
            _STATUS_CACHE[cache_key] = status
        elapsed_ms = (time.perf_counter() - started) * 1000
        _logger.debug("[BrowserDiagnostics] check deep=%s elapsed=%.1fms", deep, elapsed_ms)
        return status

    try:
        playwright_version = _playwright_version()
    except Exception as exc:
        return remember(
            BrowserDependencyStatus(
                playwright_installed=False,
                chromium_installed=False,
                available=False,
                message="Browser automation is unavailable: Playwright is not installed.",
                install_hint=_get_install_hint(),
                python_executable=python_executable,
                browsers_path=browsers_path,
                detail=f"{type(exc).__name__}: {exc}",
            )
        )

    fs_path = _find_chromium_executable_in_browsers_path(browsers_path)
    if fs_path:
        return remember(
            BrowserDependencyStatus(
                playwright_installed=True,
                chromium_installed=True,
                available=True,
                message="Browser automation dependencies are available.",
                install_hint="",
                python_executable=python_executable,
                chromium_executable_path=fs_path,
                browsers_path=browsers_path,
                playwright_version=playwright_version,
            )
        )

    if deep:
        api_path = _find_chromium_executable_via_api()
        if api_path:
            return remember(
                BrowserDependencyStatus(
                    playwright_installed=True,
                    chromium_installed=True,
                    available=True,
                    message="Browser automation dependencies are available.",
                    install_hint="",
                    python_executable=python_executable,
                    chromium_executable_path=api_path,
                    browsers_path=browsers_path,
                    playwright_version=playwright_version,
                )
            )

        cli_status = _check_chromium_via_cli(
            python_executable=python_executable,
            browsers_path=browsers_path,
            playwright_version=playwright_version,
        )
        if cli_status is not None:
            return remember(cli_status)

    detail = (
        f"Python: {python_executable}\n"
        f"Browsers path: {browsers_path or '(not found)'}\n"
        f"Playwright: {playwright_version}"
    )
    if not deep:
        detail += "\nDeep Playwright probe skipped during startup."

    return remember(
        BrowserDependencyStatus(
            playwright_installed=True,
            chromium_installed=False,
            available=False,
            message=(
                "Browser automation quick check did not find Chromium."
                if not deep
                else "Browser automation is unavailable: Playwright is installed but Chromium was not found."
            ),
            install_hint=_get_install_hint(),
            python_executable=python_executable,
            chromium_executable_path="",
            browsers_path=browsers_path,
            playwright_version=playwright_version,
            detail=detail,
        )
    )


def log_browser_diagnostics(*, deep: bool = False) -> None:
    """Log browser dependency status."""
    started = time.perf_counter()
    status = check_browser_dependencies(deep=deep)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if status.available:
        _logger.info("[BrowserDiagnostics] %s (%.0fms)", status.message, elapsed_ms)
    else:
        _logger.warning("[BrowserDiagnostics] %s (%.0fms)", status.message, elapsed_ms)
        _logger.warning("[BrowserDiagnostics] install command: %s", status.install_hint)
        _logger.info("[BrowserDiagnostics] Python: %s", status.python_executable)
        _logger.info("[BrowserDiagnostics] Browsers path: %s", status.browsers_path)

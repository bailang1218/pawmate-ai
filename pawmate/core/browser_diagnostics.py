"""
Browser dependency diagnostics — check Playwright + Chromium availability.

Detection uses the current Python interpreter (sys.executable) and
Playwright's own executable_path API. Supports frozen/PyInstaller
environments via PAWMATE_PLAYWRIGHT_BROWSERS_PATH env var.
"""
from __future__ import annotations

import logging
import os
import sys
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pawmate.core.resource_limits import MAX_COMMAND_OUTPUT_BYTES, read_binary_fileobj_limited

_logger = logging.getLogger("pawmate")


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
    """Return install command using the current Python interpreter."""
    py = sys.executable
    return (
        f'"{py}" -m pip install playwright '
        f'&& "{py}" -m playwright install chromium'
    )


def _get_browsers_path() -> Optional[str]:
    """Resolve the Playwright browsers storage directory.

    Priority:
      1. PAWMATE_PLAYWRIGHT_BROWSERS_PATH (app-specific override)
      2. PLAYWRIGHT_BROWSERS_PATH (playwright standard)
      3. Default %USERPROFILE%\\AppData\\Local\\ms-playwright
    """
    for var in ("PAWMATE_PLAYWRIGHT_BROWSERS_PATH", "PLAYWRIGHT_BROWSERS_PATH"):
        val = os.environ.get(var)
        if val:
            return val
    # Default location
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        bp = Path(local_app_data) / "ms-playwright"
        if bp.is_dir():
            return str(bp)
    return None


def _is_frozen() -> bool:
    """Check if running in a PyInstaller / frozen exe environment."""
    return getattr(sys, "frozen", False) or bool(getattr(sys, "_MEIPASS", None))


def _find_chromium_executable_via_api() -> Optional[str]:
    """Use Playwright's own API to find the chromium executable path.

    Returns the path string if found, None on failure.
    Does NOT launch the browser.
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            # Access executable_path — available on chromium browser type
            chromium = p.chromium
            # In newer Playwright, executable_path may not be directly exposed.
            # Try common attribute names.
            for attr in ("executable_path", "_executable_path"):
                val = getattr(chromium, attr, None)
                if val:
                    return str(val)

            # Fallback: check the browsers path for chromium-* directories
            browsers_path = _get_browsers_path()
            if browsers_path:
                bp = Path(browsers_path)
                chrome_dirs = sorted(bp.glob("chromium-*"))
                if chrome_dirs:
                    # Find the actual chrome executable inside
                    for cd in reversed(chrome_dirs):  # newest first
                        chrome_exe = cd / "chrome-win64" / "chrome.exe"
                        if chrome_exe.exists():
                            return str(chrome_exe)
                        chrome_exe = cd / "chrome-win" / "chrome.exe"
                        if chrome_exe.exists():
                            return str(chrome_exe)
                        chrome_exe = cd / "chrome" / "chrome.exe"
                        if chrome_exe.exists():
                            return str(chrome_exe)
    except Exception as exc:
        _logger.debug("[BrowserDiagnostics] API path lookup failed: %s", exc)
    return None


def check_browser_dependencies() -> BrowserDependencyStatus:
    """Check whether Playwright + Chromium are available.

    Uses the current Python interpreter (sys.executable) throughout,
    not a system-wide Python. Returns a structured status without
    launching the browser or making network requests.
    """
    python_executable = sys.executable
    browsers_path = _get_browsers_path() or ""
    playwright_version = ""
    chromium_executable_path = ""

    # ── 1. Check Playwright package ────────────────────────────
    try:
        import playwright

        playwright_version = getattr(playwright, "__version__", "unknown")
    except ImportError as exc:
        return BrowserDependencyStatus(
            playwright_installed=False,
            chromium_installed=False,
            available=False,
            message="浏览器自动化功能不可用：未安装 Playwright。",
            install_hint=_get_install_hint(),
            python_executable=python_executable,
            browsers_path=browsers_path,
            detail=f"ImportError: {exc}",
        )

    # ── 2. Try Playwright API to find chromium ─────────────────
    api_path = _find_chromium_executable_via_api()
    if api_path:
        chromium_executable_path = api_path
        return BrowserDependencyStatus(
            playwright_installed=True,
            chromium_installed=True,
            available=True,
            message="浏览器自动化依赖可用。",
            install_hint="",
            python_executable=python_executable,
            chromium_executable_path=chromium_executable_path,
            browsers_path=browsers_path,
            playwright_version=playwright_version,
        )

    # ── 3. Fallback: check ms-playwright/chromium-* directories ──
    if browsers_path:
        bp = Path(browsers_path)
        chrome_dirs = sorted(bp.glob("chromium-*"))
        if chrome_dirs:
            # Found chromium-* directories — test existence of chrome.exe
            for cd in reversed(chrome_dirs):
                for candidate in ("chrome-win64\\chrome.exe", "chrome-win\\chrome.exe", "chrome\\chrome.exe", "chrome.exe"):
                    exe = cd / candidate
                    if exe.exists():
                        chromium_executable_path = str(exe)
                        return BrowserDependencyStatus(
                            playwright_installed=True,
                            chromium_installed=True,
                            available=True,
                            message="浏览器自动化依赖可用。",
                            install_hint="",
                            python_executable=python_executable,
                            chromium_executable_path=chromium_executable_path,
                            browsers_path=browsers_path,
                            playwright_version=playwright_version,
                        )

    # ── 4. Fallback: check via subprocess ──────────────────────
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
        if result.returncode == 0 and "chromium" in stdout:
            return BrowserDependencyStatus(
                playwright_installed=True,
                chromium_installed=True,
                available=True,
                message="浏览器自动化依赖可用。",
                install_hint="",
                python_executable=python_executable,
                chromium_executable_path="(via playwright CLI)",
                browsers_path=browsers_path,
                playwright_version=playwright_version,
            )
    except Exception as exc:
        _logger.debug("[BrowserDiagnostics] subprocess check: %s", exc)

    # ── 5. Chromium not found ──────────────────────────────────
    return BrowserDependencyStatus(
        playwright_installed=True,
        chromium_installed=False,
        available=False,
        message="浏览器自动化功能不可用：Playwright 已安装但 Chromium 浏览器未安装。",
        install_hint=_get_install_hint(),
        python_executable=python_executable,
        chromium_executable_path="",
        browsers_path=browsers_path,
        playwright_version=playwright_version,
        detail=(f"Python: {python_executable}\n"
                f"Browsers path: {browsers_path or '(not found)'}\n"
                f"Playwright: {playwright_version}"),
    )


def log_browser_diagnostics() -> None:
    """Log browser dependency status at startup (non-blocking)."""
    status = check_browser_dependencies()
    if status.available:
        _logger.info("[BrowserDiagnostics] %s", status.message)
    else:
        _logger.warning("[BrowserDiagnostics] %s", status.message)
        _logger.warning("[BrowserDiagnostics] 安装命令: %s", status.install_hint)
        _logger.info("[BrowserDiagnostics] Python: %s", status.python_executable)
        _logger.info("[BrowserDiagnostics] Browsers path: %s", status.browsers_path)

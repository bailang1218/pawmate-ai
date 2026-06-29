"""Playwright-backed browser tools.

The browser layer is intentionally stateful: PawMate keeps exactly one
authoritative current session/page. All browser_* tools operate on that page,
so navigation summaries, clicks, screenshots, and current-URL reports describe
the same Playwright context.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from pawmate.core.browser_diagnostics import check_browser_dependencies
from pawmate.core.security_service import SecurityService
from pawmate.storage.app_paths import get_app_paths
from pawmate.tools.browser_preflight import ensure_native_profile_unlocked
from pawmate.tools.registry import (
    APPROVAL_CONFIRM,
    ToolDef,
    ToolRegistry,
)

logger = logging.getLogger("pawmate.browser")


class BrowserDependencyError(RuntimeError):
    """Raised when Playwright or Chromium are not available."""


class BrowserIntentDowngrade(BrowserDependencyError):
    """Raised when an explicit browser intent would require a profile downgrade."""

    def __init__(
        self,
        message: str,
        *,
        intent: str,
        fallback: str,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.intent = intent
        self.fallback = fallback
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True)
class BrowserUseSettings:
    enabled: bool = True
    browser: str = "edge"
    headless: bool = False
    timeout: int = 180
    attach_mode: str = "auto"
    cdp_url: str = ""
    profile_directory: str = "auto"
    profile_name: str = "auto"
    automation_level: str = "standard"


@dataclass
class BrowserSession:
    visibility: str
    browser: Any
    context: Any
    page: Any = None
    attached: bool = False
    persistent_context: bool = False
    notice: str = ""
    intent: str = ""
    intent_downgraded: bool = False
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    mode: str = "managed"
    browser_type: str = ""
    cdp_url: str = ""
    profile_dir: str = ""
    page_id: str = ""
    target_id: str = ""
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)


_BACKGROUND_HINTS = (
    "download", "下载", "install", "installer", "静默", "后台", "silent",
)
_FOREGROUND_HINTS = (
    "login", "log in", "signin", "sign in", "登录", "登入", "管理后台",
    "客服", "闲鱼", "视频", "播放", "看", "watch", "bilibili",
    "youtube", "douyin", "抖音", "xianyu", "taobao", "淘宝",
)

_browser_lock = asyncio.Lock()
_playwright_ref = None
_current_session: BrowserSession | None = None
_browser_security_service: SecurityService | None = None
_DEFAULT_CDP_PORT = 9222


def _check_deps() -> None:
    status = check_browser_dependencies()
    if not status.available:
        msg = status.message
        if status.install_hint:
            msg += f"\n安装命令: {status.install_hint}"
        raise BrowserDependencyError(msg)


def _load_browser_settings() -> BrowserUseSettings:
    """Read browser_use settings from config.json with env overrides."""
    data: dict[str, Any] = {}
    try:
        import pawmate.config as config

        config_path = Path(config.BASE_DIR) / "config.json"
        if config_path.exists():
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    except Exception as exc:
        logger.debug("[Browser] config load failed: %s", exc)

    raw = data.get("tools", {}).get("browser_use", {})
    raw = raw if isinstance(raw, dict) else {}

    env_headless = os.getenv("PAWMATE_BROWSER_HEADLESS")
    headless = bool(raw.get("headless", False))
    if env_headless is not None:
        headless = env_headless.lower() in ("1", "true", "yes", "on")

    try:
        timeout = int(raw.get("timeout", 180))
    except Exception:
        timeout = 180
    automation_level = str(raw.get("automation_level", "standard") or "standard").lower()
    if automation_level not in {"conservative", "standard", "aggressive"}:
        automation_level = "standard"

    return BrowserUseSettings(
        enabled=bool(raw.get("enabled", True)),
        browser=str(raw.get("browser", "edge") or "edge").lower(),
        headless=headless,
        timeout=max(5, min(timeout, 600)),
        attach_mode=str(raw.get("attach_mode", "auto") or "auto").lower(),
        cdp_url=str(raw.get("cdp_url", "") or "").strip(),
        profile_directory=str(raw.get("profile_directory", "auto") or "auto").strip(),
        profile_name=str(raw.get("profile_name", "auto") or "auto").strip(),
        automation_level=automation_level,
    )


def _classify_visibility(mode: str = "auto", purpose: str = "", url: str = "") -> str:
    """Return background or foreground for a browser task."""
    requested = (mode or "auto").strip().lower()
    if requested in {"background", "silent", "headless", "hidden"}:
        return "background"
    if requested in {"foreground", "visible", "open", "headed", "front"}:
        return "foreground"

    haystack = f"{purpose} {url}".lower()
    if any(h.lower() in haystack for h in _FOREGROUND_HINTS):
        return "foreground"
    if any(h.lower() in haystack for h in _BACKGROUND_HINTS):
        return "background"
    if re.search(r"\.(?:exe|msi|zip|7z|rar|dmg|pkg|deb|rpm)(?:[?#].*)?$", url.lower()):
        return "background"
    return "foreground"


def _channel_for_browser(browser: str) -> Optional[str]:
    if browser == "edge":
        return "msedge"
    if browser == "chrome":
        return "chrome"
    return None


def _normalize_cdp_url(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://", "ws://", "wss://")):
        value = f"http://{value}"
    return value.replace("://localhost", "://127.0.0.1")


def _cdp_url_for_settings(settings: BrowserUseSettings) -> str:
    env_url = os.getenv("PAWMATE_BROWSER_CDP_URL", "").strip()
    if env_url:
        return _normalize_cdp_url(env_url)
    if settings.cdp_url:
        return _normalize_cdp_url(settings.cdp_url)
    return f"http://127.0.0.1:{_DEFAULT_CDP_PORT}"


def _is_local_cdp_url(cdp_url: str) -> bool:
    parsed = urlparse(_normalize_cdp_url(cdp_url))
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _cdp_port(cdp_url: str) -> int:
    parsed = urlparse(_normalize_cdp_url(cdp_url))
    return parsed.port or _DEFAULT_CDP_PORT


def _browser_user_data_root(browser: str) -> Optional[Path]:
    if sys.platform != "win32":
        return None
    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        return None
    browser = (browser or "edge").strip().lower() or "edge"
    if browser == "chrome":
        path = Path(local_app_data) / "Google" / "Chrome" / "User Data"
    else:
        path = Path(local_app_data) / "Microsoft" / "Edge" / "User Data"
    return path if path.exists() else None


def _managed_profile_dir(settings: BrowserUseSettings) -> Path:
    browser = (settings.browser or "edge").strip().lower() or "edge"
    return get_app_paths().data_root / "browser_profiles" / f"foreground_{browser}"


def _debug_profile_dir(settings: BrowserUseSettings) -> Path:
    """Dedicated, persistent user-data-dir that PawMate owns for CDP.

    This is the key to "CDP on by default": we must NOT reuse the user's native
    user-data-dir, because if their browser is already running on it, launching a
    new instance with --remote-debugging-port just hands off to the existing
    process and exits without ever opening the debug port. A separate profile has
    no running instance, so the port opens reliably, and it persists so the user
    only logs in once.
    """
    browser = (settings.browser or "edge").strip().lower() or "edge"
    return get_app_paths().data_root / "browser_profiles" / f"cdp_{browser}"


def _debug_profile_is_fresh(profile_dir: Path) -> bool:
    """True when the debug profile has no signed-in data yet (first run)."""
    try:
        return not (profile_dir / "Default" / "Cookies").exists()
    except Exception:
        return True


def _profile_directory_requests_native(settings: BrowserUseSettings) -> bool:
    return (settings.profile_directory or "auto").strip().lower() in {
        "native",
        "user",
        "system",
        "local",
    }


def _wants_native_attach(settings: BrowserUseSettings) -> bool:
    return settings.attach_mode == "attach" or _profile_directory_requests_native(settings)


def _cdp_profile_for_launch(settings: BrowserUseSettings) -> tuple[Path, str, bool]:
    """Return user-data-dir/profile-name for a browser started with CDP enabled.

    profile_directory=native means PawMate should use the real local
    Edge/Chrome profile instead of a PawMate-owned Playwright/debug profile.
    This carries over cookies, login state, extensions, history, and other
    browser data, but it only works when that profile is not already locked by
    a normal browser process.
    """
    if _profile_directory_requests_native(settings):
        native_root = _browser_user_data_root(settings.browser)
        if native_root is not None:
            return native_root, _read_native_profile_name(settings, native_root), True
        logger.warning("[Browser] native profile requested but no native %s profile was found", settings.browser)
    return _debug_profile_dir(settings), "Default", False


def _cdp_launch_profile_is_fresh(settings: BrowserUseSettings) -> bool:
    profile_dir, _profile_name, native_profile = _cdp_profile_for_launch(settings)
    if native_profile:
        return False
    return _debug_profile_is_fresh(profile_dir)


def _read_native_profile_name(settings: BrowserUseSettings, user_data_root: Path) -> str:
    configured = (settings.profile_name or "auto").strip()
    if configured.lower() not in {"", "auto", "default"}:
        return configured
    local_state = user_data_root / "Local State"
    if local_state.exists():
        try:
            data = json.loads(local_state.read_text(encoding="utf-8"))
            profile = data.get("profile", {}) if isinstance(data.get("profile", {}), dict) else {}
            last_used = str(profile.get("last_used", "") or "").strip()
            if last_used and (user_data_root / last_used).exists():
                return last_used
            desktop = data.get("desktop_mode", {}) if isinstance(data.get("desktop_mode", {}), dict) else {}
            preferred = str(desktop.get("preferred_profile", "") or "").strip()
            preferred_name = Path(preferred).name if preferred else ""
            if preferred_name and (user_data_root / preferred_name).exists():
                return preferred_name
        except Exception as exc:
            logger.debug("[Browser] failed to read native profile name: %s", exc)
    return "Default"


def _native_profile_launch_args(settings: BrowserUseSettings) -> list[str]:
    root = _browser_user_data_root(settings.browser)
    if root is None:
        return []
    profile_name = _read_native_profile_name(settings, root)
    return [
        f"--user-data-dir={root}",
        f"--profile-directory={profile_name}",
    ]


def _persistent_context_args(settings: BrowserUseSettings, profile_dir: str) -> list[str]:
    args = ["--disable-blink-features=AutomationControlled"]
    native_root = _browser_user_data_root(settings.browser)
    try:
        if native_root and Path(profile_dir).resolve() == native_root.resolve():
            args.append(f"--profile-directory={_read_native_profile_name(settings, native_root)}")
    except Exception:
        pass
    return args


def _extract_executable_from_command(command: str) -> Optional[str]:
    command = os.path.expandvars(str(command or "").strip())
    if not command:
        return None
    quoted = re.match(r'^\s*"([^"]+?\.exe)"', command, flags=re.IGNORECASE)
    if quoted:
        return quoted.group(1)
    unquoted = re.match(r"^\s*(.+?\.exe)(?:\s|$)", command, flags=re.IGNORECASE)
    if unquoted:
        return unquoted.group(1).strip()
    return None


def _is_chromium_browser_executable(path: str | None) -> bool:
    name = Path(path or "").name.lower()
    return name in {
        "chrome.exe",
        "msedge.exe",
        "brave.exe",
        "vivaldi.exe",
        "opera.exe",
        "opera_gx.exe",
        "chromium.exe",
    }


def _windows_default_browser_executable() -> Optional[str]:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except Exception:
        return None

    prog_id = ""
    for scheme in ("https", "http"):
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                rf"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\{scheme}\UserChoice",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "ProgId")
                prog_id = str(value or "").strip()
                if prog_id:
                    break
        except OSError:
            continue
    if not prog_id:
        return None

    command_keys = (
        (winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}\shell\open\command"),
        (winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command"),
    )
    for hive, subkey in command_keys:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                command, _ = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        executable = _extract_executable_from_command(str(command))
        if executable and Path(executable).exists() and _is_chromium_browser_executable(executable):
            return executable
    return None


def _common_browser_executables(browser: str) -> list[str]:
    if sys.platform != "win32":
        return []
    local_app_data = os.getenv("LOCALAPPDATA", "")
    program_files = os.getenv("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
    if browser == "chrome":
        return [
            str(Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            str(Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            str(Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ]
    return [
        str(Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
        str(Path(program_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
        str(Path(local_app_data) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
    ]


def _remote_debug_launch_candidates(settings: BrowserUseSettings) -> list[str]:
    candidates: list[str] = []
    preferred = (settings.browser or "edge").strip().lower() or "edge"
    candidates.extend(_common_browser_executables(preferred))
    default_executable = _windows_default_browser_executable()
    if default_executable:
        candidates.append(default_executable)
    if preferred != "edge":
        candidates.extend(_common_browser_executables("edge"))

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = os.path.normcase(os.path.abspath(os.path.expandvars(candidate)))
        if normalized in seen:
            continue
        seen.add(normalized)
        if Path(candidate).exists() and _is_chromium_browser_executable(candidate):
            deduped.append(candidate)
    return deduped


def _browser_process_name(browser: str) -> str:
    browser = (browser or "edge").strip().lower() or "edge"
    return "chrome.exe" if browser == "chrome" else "msedge.exe"


def _browser_process_running(browser: str) -> bool:
    if sys.platform != "win32":
        return False
    process_name = _browser_process_name(browser)
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.lower()
        return process_name.lower() in out
    except Exception:
        return False


def _debug_launch_suggestion(settings: BrowserUseSettings, cdp_url: str) -> dict[str, str]:
    port = _cdp_port(cdp_url)
    exe_name = "chrome.exe" if (settings.browser or "").strip().lower() == "chrome" else "msedge.exe"
    return {
        "windows_cmd": (
            f'cmd /c start "" "{exe_name}" --remote-debugging-port={port} '
            '--user-data-dir="%TEMP%\\pawmate-edge-debug"'
        ),
        "note": (
            "Start the browser with a remote debugging port, then retry browser_goto(use_my_login=true). "
            "PawMate will not silently fall back to a managed profile for login-state tasks."
        ),
    }


def _native_mount_diagnostics(settings: BrowserUseSettings, cdp_url: str) -> dict[str, Any]:
    try:
        from pawmate.tools.cdp_attach import probe_cdp, probe_cdp_targets
    except Exception:
        probe_cdp = None
        probe_cdp_targets = None

    version = probe_cdp(cdp_url) if probe_cdp else None
    targets = probe_cdp_targets(cdp_url) if probe_cdp_targets else None
    candidates = _remote_debug_launch_candidates(settings)
    native_root = _browser_user_data_root(settings.browser)
    process_running = _browser_process_running(settings.browser)
    return {
        "cdp_url": cdp_url,
        "cdp_port": _cdp_port(cdp_url),
        "is_windows": sys.platform == "win32",
        "browser": settings.browser,
        "browser_process_running": process_running,
        "possible_profile_lock": bool(process_running and version is None),
        "version_ok": version is not None,
        "version": version or {},
        "targets_ok": targets is not None,
        "target_count": len(targets or []),
        "targets": [
            {
                "id": str(target.get("id", "")),
                "type": str(target.get("type", "")),
                "url": str(target.get("url", ""))[:300],
                "title": str(target.get("title", ""))[:200],
            }
            for target in (targets or [])[:10]
            if isinstance(target, dict)
        ],
        "native_profile_dir": str(native_root or ""),
        "browser_executable_found": bool(candidates),
        "browser_executable_candidates": candidates[:5],
        "suggestion": _debug_launch_suggestion(settings, cdp_url),
    }


def _launch_debuggable_browser(
    settings: BrowserUseSettings,
    cdp_url: str,
    *,
    force_debug_profile: bool = False,
) -> bool:
    if sys.platform != "win32":
        return False

    port = _cdp_port(cdp_url)
    if force_debug_profile:
        profile_dir, profile_name, native_profile = _debug_profile_dir(settings), "Default", False
    else:
        profile_dir, profile_name, native_profile = _cdp_profile_for_launch(settings)
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        f"--profile-directory={profile_name}",
        "--no-first-run",
        "--no-default-browser-check",
        "--restore-last-session",
        "--new-window",
        "about:blank",
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for executable in _remote_debug_launch_candidates(settings):
        try:
            subprocess.Popen(
                [executable, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creationflags,
            )
            logger.info(
                "[Browser] launched debuggable browser: %s port=%s profile=%s native_profile=%s forced_debug_profile=%s",
                executable,
                port,
                profile_dir,
                native_profile,
                force_debug_profile,
            )
            return True
        except Exception as exc:
            logger.warning("[Browser] failed to launch debuggable browser %s: %s", executable, exc)
    return False


def _foreground_launch_candidates(settings: BrowserUseSettings) -> list[tuple[str, dict[str, Any]]]:
    candidates: list[tuple[str, dict[str, Any]]] = []
    default_executable = _windows_default_browser_executable()
    if default_executable:
        candidates.append(("system default browser", {"executable_path": default_executable}))
    channel = _channel_for_browser(settings.browser)
    if channel:
        candidates.append((f"{settings.browser} channel", {"channel": channel}))
    if (settings.browser or "").strip().lower() == "chromium" or os.getenv("PAWMATE_BROWSER_ALLOW_BUNDLED", "").lower() in {"1", "true", "yes", "on"}:
        candidates.append(("bundled chromium", {}))
    deduped: list[tuple[str, dict[str, Any]]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for label, extra in candidates:
        key = tuple(sorted((k, str(v)) for k, v in extra.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((label, extra))
    return deduped


def _resolve_profile_directory(settings: BrowserUseSettings, visibility: str) -> Optional[str]:
    raw = (settings.profile_directory or "").strip()
    if visibility != "foreground" or raw.lower() == "none":
        return None
    if raw.lower() in {"", "auto", "default"}:
        # Never the native user-data-dir by default: it is locked while the user's
        # own browser is open, so launch_persistent_context fails and collapses to
        # a throwaway bundled chromium. Use PawMate's own persistent profile.
        path = _debug_profile_dir(settings)
    elif raw.lower() == "native":
        # Explicit opt-in only (will fail loudly if the native profile is busy).
        path = _browser_user_data_root(settings.browser) or _debug_profile_dir(settings)
    elif raw.lower() == "managed":
        path = _managed_profile_dir(settings)
    else:
        path = Path(raw).expanduser()
    if not path.is_absolute():
        path = get_app_paths().data_root / "browser_profiles" / raw
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _browser_context_options() -> dict[str, Any]:
    return {
        "accept_downloads": True,
        "viewport": {"width": 1280, "height": 800},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    }


async def _ensure_playwright():
    global _playwright_ref
    if _playwright_ref is None:
        from playwright.async_api import async_playwright

        _playwright_ref = await async_playwright().__aenter__()
    return _playwright_ref


async def _launch_persistent_context_with_fallback(
    pw,
    settings: BrowserUseSettings,
    kwargs: dict[str, Any],
    profile_dir: str,
) -> Any:
    last_error: Exception | None = None
    for label, extra in _foreground_launch_candidates(settings):
        try:
            context = await pw.chromium.launch_persistent_context(profile_dir, **{**kwargs, **extra})
            logger.info("[Browser] launched foreground persistent context via %s: %s", label, profile_dir)
            return context
        except Exception as exc:
            last_error = exc
            logger.warning("[Browser] foreground launch via %s failed; trying fallback: %s", label, exc)
    assert last_error is not None
    raise last_error


async def _launch_browser_with_fallback(
    pw,
    settings: BrowserUseSettings,
    visibility: str,
    kwargs: dict[str, Any],
) -> tuple[Any, str]:
    if visibility != "foreground":
        browser = await pw.chromium.launch(**kwargs)
        return browser, "bundled chromium"

    last_error: Exception | None = None
    for label, extra in _foreground_launch_candidates(settings):
        try:
            browser = await pw.chromium.launch(**{**kwargs, **extra})
            logger.info("[Browser] launched foreground session via %s", label)
            return browser, label
        except Exception as exc:
            last_error = exc
            logger.warning("[Browser] foreground launch via %s failed; trying fallback: %s", label, exc)
    assert last_error is not None
    raise last_error


async def _connect_cdp_session(
    pw,
    cdp_url: str,
    context_options: dict[str, Any],
    visibility: str,
    *,
    settings: BrowserUseSettings | None = None,
    session_mode: str = "native",
) -> BrowserSession:
    browser = await pw.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0] if browser.contexts else await browser.new_context(**context_options)
    logger.info("[Browser] attached to CDP endpoint: %s", cdp_url)
    return BrowserSession(
        visibility=visibility,
        browser=browser,
        context=context,
        attached=True,
        mode=session_mode,
        browser_type=(settings.browser if settings else ""),
        cdp_url=cdp_url,
        profile_dir=str(_browser_user_data_root(settings.browser) or "") if settings else "",
    )


async def _wait_for_cdp_session(
    pw,
    cdp_url: str,
    context_options: dict[str, Any],
    visibility: str,
    *,
    settings: BrowserUseSettings | None = None,
    timeout_s: float = 8.0,
) -> BrowserSession:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return await _connect_cdp_session(
                pw,
                cdp_url,
                context_options,
                visibility,
                settings=settings,
                session_mode="native",
            )
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.25)
    assert last_error is not None
    raise last_error


async def _connect_or_launch(settings: BrowserUseSettings, visibility: str) -> BrowserSession:
    from pawmate.tools.cdp_attach import (
        _cdp_port,
        _poll_probe,
        any_edge_running,
        launch_debug_edge_real_profile,
        probe_cdp,
    )

    pw = await _ensure_playwright()
    # Visibility is task-level: background must stay quiet, foreground must
    # be visible so the user can log in, watch, or take over.
    headless = visibility == "background"
    context_options = _browser_context_options()
    downloads_dir = get_app_paths().downloads_dir
    downloads_dir.mkdir(parents=True, exist_ok=True)
    fallback_notice = ""

    if _wants_native_attach(settings):
        cdp_url = _cdp_url_for_settings(settings)
        info = await asyncio.to_thread(probe_cdp, cdp_url)
        if info is None and not await asyncio.to_thread(any_edge_running):
            await asyncio.to_thread(launch_debug_edge_real_profile, _cdp_port(cdp_url))
            info = await asyncio.to_thread(_poll_probe, cdp_url, 12.0)
        diagnostics = None
        if info is None:
            diagnostics = await asyncio.to_thread(_native_mount_diagnostics, settings, cdp_url)
            raise BrowserIntentDowngrade(
                "未找到可接管的调试浏览器。请用「Edge (调试模式)」快捷方式启动你的 Edge"
                "（或运行 setup_debug_edge.ps1），再重试。PawMate 不会改用没有登录态的空浏览器。",
                intent="native",
                fallback="(none)",
                diagnostics=diagnostics,
            )
        return await _connect_cdp_session(
            pw,
            cdp_url,
            context_options,
            visibility,
            settings=settings,
            session_mode="native",
        )

    if settings.attach_mode in {"auto", "prefer", "attach"}:
        cdp_url = _cdp_url_for_settings(settings)
        try:
            return await _connect_cdp_session(
                pw,
                cdp_url,
                context_options,
                visibility,
                settings=settings,
                session_mode="native",
            )
        except Exception as attach_exc:
            logger.info("[Browser] CDP attach failed at %s: %s", cdp_url, attach_exc)
            launched = False
            fresh = False
            if settings.attach_mode in {"prefer", "attach"} and _is_local_cdp_url(cdp_url):
                logger.info("[Browser] trying explicit CDP browser launch")
                if _profile_directory_requests_native(settings):
                    await _preflight_native_profile(settings, cdp_url)
                fresh = _cdp_launch_profile_is_fresh(settings)
                launched = await asyncio.to_thread(_launch_debuggable_browser, settings, cdp_url)
            if launched:
                try:
                    session = await _wait_for_cdp_session(
                        pw,
                        cdp_url,
                        context_options,
                        visibility,
                        settings=settings,
                        timeout_s=20.0,
                    )
                    if fresh:
                        session.notice = (
                            "已用调试模式打开浏览器并通过 CDP 接管。这是 PawMate 专用的浏览器副本，"
                            "首次请在这个窗口登录一次需要的网站，之后会一直保持登录、无需再登。"
                        )
                    return session
                except Exception as launch_attach_exc:
                    attach_exc = launch_attach_exc
                    logger.warning("[Browser] debuggable browser did not expose CDP: %s", launch_attach_exc)
                    if _profile_directory_requests_native(settings) and _is_local_cdp_url(cdp_url):
                        message = (
                            f"Cannot take over the native browser session at {cdp_url}; "
                            "PawMate will not silently switch to its managed profile."
                        )
                        raise BrowserIntentDowngrade(
                            message,
                            intent="native",
                            fallback="managed",
                            diagnostics=await asyncio.to_thread(_native_mount_diagnostics, settings, cdp_url),
                        ) from attach_exc
            if settings.attach_mode == "attach":
                native_requested = _profile_directory_requests_native(settings)
                native_hint = ""
                if native_requested:
                    native_hint = (
                        " PawMate is configured to use a real local browser profile. "
                        "Switch browser_use.profile_directory to auto to use PawMate's dedicated persistent Edge profile."
                    )
                message = (
                    f"Cannot attach to a controllable browser at {cdp_url}. "
                    "Use browser_use.attach_mode=auto so PawMate can open its dedicated persistent real Edge profile."
                    f"{native_hint}"
                )
                raise BrowserDependencyError(message) from attach_exc
            if settings.cdp_url:
                logger.exception("[Browser] CDP attach failed; launching a new browser")
            else:
                logger.info("[Browser] CDP auto attach unavailable; launching persistent local browser")
            fallback_notice = "浏览器接管失败，已自动改用 PawMate 专属持久化真实 Edge 窗口重试。"

    profile_dir = _resolve_profile_directory(settings, visibility)
    if profile_dir:
        kwargs = {
            **context_options,
            "headless": headless,
            "downloads_path": str(downloads_dir),
            "args": _persistent_context_args(settings, profile_dir),
        }
        try:
            context = await _launch_persistent_context_with_fallback(pw, settings, kwargs, profile_dir)
        except Exception as exc:
            native_root = _browser_user_data_root(settings.browser)
            try:
                is_native_profile = native_root is not None and Path(profile_dir).resolve() == native_root.resolve()
            except Exception:
                is_native_profile = False
            if is_native_profile:
                raw_profile_mode = (settings.profile_directory or "auto").strip().lower()
                if raw_profile_mode in {"", "auto", "default"}:
                    managed_profile_dir = _managed_profile_dir(settings)
                    managed_profile_dir.mkdir(parents=True, exist_ok=True)
                    managed_notice = (
                        "Native browser profile is busy or unavailable; "
                        "using a PawMate-managed browser profile instead."
                    )
                    fallback_notice = f"{fallback_notice} {managed_notice}".strip()
                    managed_kwargs = {
                        **context_options,
                        "headless": headless,
                        "downloads_path": str(downloads_dir),
                        "args": _persistent_context_args(settings, str(managed_profile_dir)),
                    }
                    try:
                        context = await _launch_persistent_context_with_fallback(
                            pw,
                            settings,
                            managed_kwargs,
                            str(managed_profile_dir),
                        )
                        return BrowserSession(
                            visibility=visibility,
                            browser=context.browser,
                            context=context,
                            persistent_context=True,
                            notice=fallback_notice,
                            mode="managed",
                            browser_type=settings.browser,
                            profile_dir=str(managed_profile_dir),
                        )
                    except Exception as managed_exc:
                        logger.warning("[Browser] managed foreground profile fallback failed: %s", managed_exc)
                profile_name = _read_native_profile_name(settings, native_root)
                message = (
                    f"Cannot open the native {settings.browser} profile '{profile_name}'. "
                    f"Profile directory: {profile_dir}. "
                    "Use browser_use.profile_directory=auto to open PawMate's dedicated persistent real Edge profile instead."
                )
                raise BrowserDependencyError(message) from exc
            raise
        return BrowserSession(
            visibility=visibility,
            browser=context.browser,
            context=context,
            persistent_context=True,
            notice=fallback_notice,
            mode="managed",
            browser_type=settings.browser,
            profile_dir=str(profile_dir),
        )

    launch_kwargs: dict[str, Any] = {
        "headless": headless,
        "downloads_path": str(downloads_dir),
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if visibility == "background":
        channel = _channel_for_browser(settings.browser)
        if channel:
            launch_kwargs["channel"] = channel
    browser, launch_label = await _launch_browser_with_fallback(pw, settings, visibility, launch_kwargs)
    context = await browser.new_context(**context_options)
    logger.info("[Browser] launched %s session via %s (headless=%s)", visibility, launch_label, headless)
    return BrowserSession(
        visibility=visibility,
        browser=browser,
        context=context,
        notice=fallback_notice,
        mode="managed",
        browser_type=settings.browser,
    )


async def _preflight_native_profile(settings: BrowserUseSettings, cdp_url: str) -> None:
    if sys.platform != "win32":
        return
    native_root = _browser_user_data_root(settings.browser)
    service = _browser_security_service or SecurityService()

    async def confirm_restart() -> bool:
        return await service.require_confirmation(
            "browser_preflight",
            {
                "browser": settings.browser,
                "profile_directory": settings.profile_directory,
                "profile_name": settings.profile_name,
                "native_user_data_root": str(native_root) if native_root else "",
                "cdp_url": cdp_url,
                "port": _cdp_port(cdp_url),
                "action": "graceful_restart_native_edge",
            },
            reason=(
                "PawMate needs to close ordinary Edge and relaunch it with a "
                "debugging port to take over the requested native browser session."
            ),
        )

    try:
        await ensure_native_profile_unlocked(
            native_root,
            port=_cdp_port(cdp_url),
            confirm_restart=confirm_restart,
        )
    except Exception as exc:
        logger.warning("[Browser] native profile preflight failed: %s", exc)


async def _ensure_session(visibility: str) -> BrowserSession:
    global _current_session
    _check_deps()
    settings = _load_browser_settings()
    if not settings.enabled:
        raise BrowserDependencyError("浏览器自动化已在设置中关闭。")

    async with _browser_lock:
        session = _current_session
        if session is not None:
            try:
                if session.browser is not None and hasattr(session.browser, "is_connected"):
                    if not session.browser.is_connected():
                        raise RuntimeError("browser disconnected")
                if session.context:
                    if session.visibility == "background" and visibility == "foreground":
                        await _close_session(session)
                        _current_session = None
                    else:
                        return session
            except Exception:
                _current_session = None

        session = await _connect_or_launch(settings, visibility)
        _current_session = session
        return session


def _session_is_usable(session: BrowserSession | None) -> bool:
    if session is None or not session.context:
        return False
    try:
        if session.browser is not None and hasattr(session.browser, "is_connected"):
            return bool(session.browser.is_connected())
    except Exception:
        return False
    return True


def _browser_session_info(session: BrowserSession | None = None) -> dict[str, Any]:
    session = _current_session if session is None else session
    if session is None:
        return {}
    return {
        "session_id": session.session_id,
        "mode": session.mode,
        "browser_type": session.browser_type,
        "cdp_url": session.cdp_url,
        "profile_dir": session.profile_dir,
        "page_id": session.page_id,
        "target_id": session.target_id,
        "created_at": session.created_at,
        "last_used_at": session.last_used_at,
        "attached": bool(session.attached),
        "visibility": session.visibility,
    }


def _native_session_unavailable_result(session: BrowserSession | None = None) -> dict[str, Any]:
    settings = _load_browser_settings()
    cdp_url = session.cdp_url if session and session.cdp_url else _cdp_url_for_settings(settings)
    return {
        "ok": False,
        "operation": "browser_act",
        "error": "native_session_disconnected",
        "error_type": "native_session_disconnected",
        "message": (
            "The active native browser session is not connected. "
            "PawMate will not silently switch to a managed browser because this task requires login state."
        ),
        "session": _browser_session_info(session),
        "suggestion": _debug_launch_suggestion(settings, cdp_url),
    }


async def _ensure_attached_session(visibility: str) -> BrowserSession:
    """Return a current/attached CDP session without launching or taking over a profile."""
    global _current_session
    _check_deps()
    settings = _load_browser_settings()
    if not settings.enabled:
        raise BrowserDependencyError("Browser automation is disabled in settings.")

    async with _browser_lock:
        if _session_is_usable(_current_session):
            return _current_session

        pw = await _ensure_playwright()
        cdp_url = _cdp_url_for_settings(settings)
        try:
            session = await _connect_cdp_session(
                pw,
                cdp_url,
                _browser_context_options(),
                visibility,
                settings=settings,
                session_mode="native",
            )
        except Exception as exc:
            message = (
                f"No controllable local browser is available at {cdp_url}. "
                "Use browser_open instead to start PawMate's dedicated persistent real Edge profile. "
                "browser_list_pages only inspects an already controllable CDP session."
            )
            raise BrowserDependencyError(message) from exc
        _current_session = session
        return session


async def _close_session(session: BrowserSession | None) -> bool:
    if session is None:
        return False
    if session.attached:
        return True
    try:
        if session.context:
            await session.context.close()
        elif session.browser:
            await session.browser.close()
        return True
    except Exception:
        logger.exception("[Browser] close failed for %s", getattr(session, "visibility", "current"))
        return False


async def _get_active_page(mode: str = "auto", purpose: str = "", url: str = "") -> tuple[BrowserSession, Any]:
    visibility = _classify_visibility(mode=mode, purpose=purpose, url=url)
    session = _current_session if not url and _session_is_usable(_current_session) else None
    if session is None:
        session = await _ensure_session(visibility)

    page = await _select_existing_page(session)
    if page is None:
        page = await session.context.new_page()
    session.page = page
    session.last_used_at = time.time()
    session.page_id = str(id(page))
    try:
        session.target_id = str(getattr(page, "guid", "") or getattr(page, "_guid", "") or "")
    except Exception:
        session.target_id = ""
    if visibility == "foreground" or session.attached:
        await _bring_page_to_front(page)
    return session, page


def _page_url(page: Any) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


def _is_blank_page(page: Any) -> bool:
    url = _page_url(page).strip().lower()
    return not url or url in {"about:blank", "chrome://newtab/", "edge://newtab/"}


async def _bring_page_to_front(page: Any) -> None:
    try:
        await page.bring_to_front()
    except Exception:
        logger.debug("[Browser] bring_to_front failed", exc_info=True)


async def _select_existing_page(session: BrowserSession) -> Any | None:
    page = session.page
    if page is not None and not _page_is_closed(page):
        return page

    pages = [
        page
        for page in list(getattr(session.context, "pages", []) or [])
        if not _page_is_closed(page)
    ]
    if not pages:
        return None

    non_blank = [page for page in pages if not _is_blank_page(page)]
    return non_blank[-1] if non_blank else pages[0]


async def browser_input_action(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one browser_act action through the generic input backend ladder."""
    from pawmate.tools.browser_input import BrowserActionContext, BrowserActionExecutor

    action_payload = dict(payload or {})
    require_native = bool(action_payload.pop("_require_native_session", False))
    allow_os_input = bool(action_payload.pop("_allow_os_input", False))

    if require_native:
        session = _current_session
        if not _session_is_usable(session) or getattr(session, "mode", "") != "native":
            result = _native_session_unavailable_result(session)
            result["action"] = str(action_payload.get("action") or "")
            return result

    session, page = await _get_active_page(mode="foreground", purpose="browser_act")
    if require_native and getattr(session, "mode", "") != "native":
        result = _native_session_unavailable_result(session)
        result["action"] = str(action_payload.get("action") or "")
        return result

    context = BrowserActionContext(
        session=session,
        page=page,
        allow_os_input=allow_os_input,
        security_service=_browser_security_service,
    )
    result = await BrowserActionExecutor().execute(context, action_payload)
    session.last_used_at = time.time()
    result.setdefault("session", _browser_session_info(session))
    if result.get("ok"):
        result["session"] = _browser_session_info(session)
    return result


def _clean_text(text: str, max_len: int = 3000) -> str:
    if not text:
        return "(空)"
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        return text[:max_len] + f"\n... [截断，共 {len(text)} 字符]"
    return text


async def _safe_network_idle(page, timeout_ms: int = 10000) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass


async def _wait_until_settled(page, timeout_ms: int = 8000, quiet_ms: int = 600) -> bool:
    """Return True once visible DOM mutations have been quiet for quiet_ms."""
    try:
        await page.evaluate(
            """
            () => {
                if (window.__pawSettle) {
                    window.__pawSettle.last = Date.now();
                    return;
                }
                window.__pawSettle = { last: Date.now(), n: 0 };
                const observer = new MutationObserver((muts) => {
                    window.__pawSettle.last = Date.now();
                    window.__pawSettle.n += muts.length;
                });
                observer.observe(document.documentElement, {
                    subtree: true,
                    childList: true,
                    characterData: true,
                    attributes: true
                });
            }
            """
        )
    except Exception:
        return False

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            idle = await page.evaluate("() => Date.now() - (window.__pawSettle?.last || 0)")
        except Exception:
            return False
        if idle >= quiet_ms:
            return True
        await asyncio.sleep(0.15)
    return False


def _loading_note(settled: bool) -> str:
    if settled:
        return ""
    return "\n[注意] 页面仍在加载，内容可能不完整。"


def _session_notice(session: BrowserSession) -> str:
    if not session.notice:
        return ""
    return f"\n[提示] {session.notice}"


def _intent_downgrade_prompt(exc: BrowserIntentDowngrade) -> str:
    return (
        "无法接管真实浏览器会话，因此已停止本次浏览器操作，没有切换到 PawMate 托管 profile。\n"
        f"intent={exc.intent}; fallback={exc.fallback}\n"
        "可选操作：\n"
        "1. 关闭普通 Edge/Chrome 后，重试接管真实会话。\n"
        "2. 经用户同意后，把 browser_use.profile_directory 设置为 auto，改用 PawMate 托管 profile；首次使用需要重新登录一次。"
    )


async def _extract_page_text(page, max_len: int = 2000) -> str:
    content = await page.evaluate(
        """
        () => {
            const removals = document.querySelectorAll(
                'script, style, nav, footer, header, .sidebar, .menu, ' +
                '.advertisement, .ads, [role="navigation"], .nav, .footer, .header'
            );
            removals.forEach(el => el.remove());
            const main = document.querySelector('main, article, .content, .post, .entry, #content');
            return (main || document.body)?.innerText || '';
        }
        """
    )
    return _clean_text(content, max_len=max_len)


async def _extract_links(page, limit: int = 15) -> list[dict[str, str]]:
    return await page.evaluate(
        """
        (limit) => {
            const seen = new Set();
            const out = [];
            for (const a of document.querySelectorAll('a[href]')) {
                const href = a.href;
                if (!href || !href.startsWith('http') || seen.has(href)) continue;
                seen.add(href);
                out.push({ text: (a.innerText || '').trim().substring(0, 80), href });
                if (out.length >= limit) break;
            }
            return out;
        }
        """,
        limit,
    )


async def browser_open(url: str, mode: str = "foreground", purpose: str = "", timeout: int = 30) -> str:
    """Open a URL in a background or foreground persistent browser session."""
    visibility = _classify_visibility(mode=mode, purpose=purpose, url=url)
    try:
        session = await _ensure_session(visibility)
    except BrowserIntentDowngrade as exc:
        return _intent_downgrade_prompt(exc)
    page = await session.context.new_page()
    session.page = page
    if visibility == "foreground" or session.attached:
        await _bring_page_to_front(page)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    if visibility == "foreground" or session.attached:
        await _bring_page_to_front(page)
    settled = await _wait_until_settled(page)
    title = await page.title()
    visibility = session.visibility
    if not settled:
        visibility = f"{visibility}{_loading_note(settled)}"
    visibility = f"{visibility}{_session_notice(session)}"
    return f"已打开: {page.url}\n标题: {title}\n模式: {visibility}"


async def browser_navigate(url: str, timeout: int = 30, mode: str = "auto", purpose: str = "") -> str:
    start = time.time()
    try:
        session, page = await _get_active_page(mode=mode, purpose=purpose, url=url)
    except BrowserIntentDowngrade as exc:
        return _intent_downgrade_prompt(exc)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    if session.visibility == "foreground" or session.attached:
        await _bring_page_to_front(page)
    settled = await _wait_until_settled(page)

    title = await page.title()
    summary = await _extract_page_text(page, max_len=2000)
    links = await _extract_links(page)
    visibility = session.visibility
    visibility = f"{visibility}{_session_notice(session)}"
    result = (
        f"已打开: {page.url}\n"
        f"标题: {title}\n"
        f"模式: {visibility}\n"
        f"耗时: {time.time() - start:.1f}s\n\n"
        f"页面内容:\n{summary}\n"
    )
    result += _loading_note(settled)
    if links:
        result += f"\n页面链接 ({len(links)} 条):\n"
        for i, link in enumerate(links[:8], 1):
            text = link.get("text") or "(链接)"
            result += f"  {i}. {text[:60]} -> {link['href'][:100]}\n"
    return result


async def browser_observe(max_items: int = 80, mode: str = "auto", purpose: str = "") -> dict[str, Any]:
    """Return visible action candidates on the current page."""
    session, page = await _get_active_page(mode=mode, purpose=purpose)
    data = await page.evaluate(
        """
        (maxItems) => {
            function cssPath(el) {
                if (el.id) return '#' + CSS.escape(el.id);
                const parts = [];
                while (el && el.nodeType === Node.ELEMENT_NODE && parts.length < 5) {
                    let part = el.nodeName.toLowerCase();
                    const cls = Array.from(el.classList || []).slice(0, 2).map(c => '.' + CSS.escape(c)).join('');
                    if (cls) part += cls;
                    const parent = el.parentElement;
                    if (parent) {
                        const same = Array.from(parent.children).filter(x => x.nodeName === el.nodeName);
                        if (same.length > 1) part += `:nth-of-type(${same.indexOf(el) + 1})`;
                    }
                    parts.unshift(part);
                    el = parent;
                }
                return parts.join(' > ');
            }
            function visible(el) {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none';
            }
            const selector = [
                'a[href]', 'button', 'input', 'textarea', 'select',
                '[role="button"]', '[role="link"]', '[contenteditable="true"]'
            ].join(',');
            const items = [];
            for (const el of document.querySelectorAll(selector)) {
                if (!visible(el)) continue;
                const r = el.getBoundingClientRect();
                const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.title || '').trim();
                items.push({
                    tag: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || '',
                    text: text.substring(0, 120),
                    href: el.href || '',
                    selector: cssPath(el),
                    disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
                    bbox: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }
                });
                if (items.length >= maxItems) break;
            }
            return { url: location.href, title: document.title, items };
        }
        """,
        max(1, min(int(max_items), 200)),
    )
    data["mode"] = session.visibility
    if session.notice:
        data["message"] = session.notice
    return data


async def browser_wait_for(
    selector: str = "",
    text: str = "",
    state: str = "visible",
    timeout: int = 20,
    mode: str = "auto",
    purpose: str = "",
) -> dict[str, Any]:
    """Wait for a selector or text to appear and return page candidates on timeout."""
    _, page = await _get_active_page(mode=mode, purpose=purpose)
    timeout_ms = max(1, min(int(timeout), 120)) * 1000
    try:
        if selector:
            await page.wait_for_selector(selector, state=state, timeout=timeout_ms)
            return {"ok": True, "operation": "browser_wait_for", "matched": "selector", "selector": selector, "url": page.url}
        if text:
            await page.wait_for_function(
                "t => document.body && document.body.innerText.includes(t)",
                arg=text,
                timeout=timeout_ms,
            )
            return {"ok": True, "operation": "browser_wait_for", "matched": "text", "text": text, "url": page.url}
        return {
            "ok": False,
            "operation": "browser_wait_for",
            "error_type": "bad_args",
            "message": "selector 或 text 至少给一个",
            "url": page.url,
        }
    except Exception:
        snap = await browser_observe(max_items=40, mode=mode, purpose=purpose)
        return {
            "ok": False,
            "operation": "browser_wait_for",
            "error_type": "not_found_in_time",
            "looked_for": selector or text,
            "candidates": snap.get("items", [])[:15],
            "url": page.url,
        }


async def browser_search(query: str, site: str = "", timeout: int = 30, mode: str = "auto") -> str:
    _, page = await _get_active_page(mode=mode, purpose="search", url="")
    q = f"site:{site} {query}" if site else query
    search_url = f"https://html.duckduckgo.com/html/?q={q}"
    await page.goto(search_url, wait_until="domcontentloaded", timeout=timeout * 1000)
    settled = await _wait_until_settled(page)
    results = await page.evaluate(
        """
        () => {
            const items = [];
            const seen = new Set();
            const candidates = document.querySelectorAll('.result__a, .result__title a, h2 a, a');
            for (const a of candidates) {
                const href = a.href;
                const text = a.innerText?.trim();
                if (href && text && href.startsWith('http') && !seen.has(href) && !href.includes('duckduckgo.com')) {
                    seen.add(href);
                    items.push({ title: text.substring(0, 120), url: href });
                }
                if (items.length >= 15) break;
            }
            return items;
        }
        """
    )
    if not results:
        return f"搜索「{query}」未找到结果。{_loading_note(settled)}"
    lines = [f"搜索「{query}」结果 ({len(results)} 条):"]
    for i, item in enumerate(results, 1):
        lines.append(f"  {i}. {item['title']}\n     {item['url']}")
    return "\n".join(lines) + _loading_note(settled)


async def browser_type_and_search(
    url: str,
    input_selector: str,
    text: str,
    submit_selector: str = "",
    timeout: int = 30,
    mode: str = "auto",
    purpose: str = "",
) -> Any:
    _, page = await _get_active_page(mode=mode, purpose=purpose or text, url=url)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    try:
        await page.wait_for_selector(input_selector, timeout=8000)
    except Exception:
        snap = await browser_observe(max_items=40, mode=mode, purpose=purpose)
        return {
            "ok": False,
            "operation": "browser_type_and_search",
            "error_type": "input_selector_not_found",
            "selector": input_selector,
            "hint": "目标输入框未找到。请从 candidates 选择一个 selector，或先用 browser_wait_for 等它出现。",
            "candidates": snap.get("items", [])[:15],
            "url": page.url,
        }
    await page.click(input_selector)
    await page.fill(input_selector, "")
    await page.type(input_selector, text, delay=20)
    if submit_selector:
        await page.click(submit_selector)
    else:
        await page.keyboard.press("Enter")
    settled = await _wait_until_settled(page)
    title = await page.title()
    summary = await _extract_page_text(page, max_len=1500)
    summary = f"{summary}{_loading_note(settled)}"
    return f"已在 {url} 搜索「{text}」\n标题: {title}\n当前 URL: {page.url}\n内容摘要:\n{summary}"


async def browser_click(
    url: str = "",
    selector: str = "",
    timeout: int = 30,
    mode: str = "auto",
    purpose: str = "",
) -> Any:
    if not selector:
        return "[错误] selector 不能为空"
    _, page = await _get_active_page(mode=mode, purpose=purpose, url=url)
    if url:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    try:
        await page.wait_for_selector(selector, timeout=8000)
        await page.click(selector)
    except Exception:
        snap = await browser_observe(max_items=40, mode=mode, purpose=purpose)
        return {
            "ok": False,
            "operation": "browser_click",
            "error_type": "selector_not_found",
            "selector": selector,
            "hint": "目标未找到。请从 candidates 选择一个 selector，或先用 browser_wait_for 等它出现。",
            "candidates": snap.get("items", [])[:15],
            "url": page.url,
        }
    settled = await _wait_until_settled(page)
    title = await page.title()
    summary = await _extract_page_text(page, max_len=1500)
    summary = f"{summary}{_loading_note(settled)}"
    return f"已点击: {selector}\n当前标题: {title}\n当前 URL: {page.url}\n\n页面内容:\n{summary}"


async def browser_click_download(
    selector: str,
    url: str = "",
    destination_folder: str = "",
    filename: str = "",
    timeout: int = 60,
    mode: str = "background",
    purpose: str = "download",
    security_service: Optional[SecurityService] = None,
) -> dict[str, Any]:
    """Click an element and capture a Playwright download event."""
    _, page = await _get_active_page(mode=mode, purpose=purpose, url=url)
    if url:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    await page.wait_for_selector(selector, timeout=10000)

    target_dir = Path(destination_folder).expanduser() if destination_folder else get_app_paths().downloads_dir
    if security_service is not None:
        allowed = await security_service.require_confirmation(
            "browser_click_download",
            {
                "url": url,
                "page_url": page.url,
                "selector": selector,
                "destination_folder": destination_folder,
                "save_dir": str(target_dir),
                "filename": filename,
                "timeout": timeout,
                "mode": mode,
                "purpose": purpose,
            },
            reason="browser_click_download clicks a page element and writes a browser download to disk",
        )
        if not allowed:
            return {
                "ok": False,
                "operation": "browser_click_download",
                "error_type": "approval_denied",
                "message": "browser_click_download was not approved",
                "url": page.url,
            }
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        async with page.expect_download(timeout=timeout * 1000) as download_info:
            await page.click(selector)
        download = await download_info.value
    except Exception as exc:
        return {
            "ok": False,
            "operation": "browser_click_download",
            "message": f"点击后未捕获到浏览器下载事件: {exc}",
            "url": page.url,
        }

    suggested = download.suggested_filename or "download.bin"
    target_name = filename.strip() or suggested
    target_path = target_dir / target_name
    if target_path.exists():
        stem, suffix = target_path.stem, target_path.suffix
        idx = 1
        while target_path.exists():
            target_path = target_dir / f"{stem}_{idx}{suffix}"
            idx += 1
    await download.save_as(str(target_path))
    size = target_path.stat().st_size if target_path.exists() else 0
    return {
        "ok": target_path.exists(),
        "operation": "browser_click_download",
        "path": str(target_path),
        "filename": target_path.name,
        "size_bytes": size,
        "source_url": download.url,
        "mode": _classify_visibility(mode=mode, purpose=purpose, url=url),
        "postcondition": {"file_exists": target_path.exists(), "size_bytes": size},
    }


async def browser_fill_form(
    url: str,
    fields: str,
    submit_selector: str = "",
    timeout: int = 30,
    mode: str = "foreground",
    purpose: str = "form",
) -> Any:
    field_dict = _parse_browser_fill_form_fields(fields)
    _, page = await _get_active_page(mode=mode, purpose=purpose, url=url)
    if not isinstance(field_dict, dict):
        return "[错误] fields 必须是 JSON 对象"
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    filled = []
    failed = []
    for selector, value in field_dict.items():
        try:
            await page.wait_for_selector(selector, timeout=5000)
            await page.click(selector)
            await page.fill(selector, "")
            await page.type(selector, str(value), delay=10)
            filled.append(selector)
        except Exception as exc:
            failed.append(selector)
            logger.warning("[Browser] fill failed selector=%s: %s", selector, exc)
    if not filled and failed:
        snap = await browser_observe(max_items=40, mode=mode, purpose=purpose)
        return {
            "ok": False,
            "operation": "browser_fill_form",
            "error_type": "form_selectors_not_found",
            "selectors": failed,
            "hint": "表单字段全部未找到。请从 candidates 选择 selector，或先用 browser_wait_for 等动态表单出现。",
            "candidates": snap.get("items", [])[:15],
            "url": page.url,
        }
    if submit_selector:
        try:
            await page.click(submit_selector)
        except Exception:
            snap = await browser_observe(max_items=40, mode=mode, purpose=purpose)
            return {
                "ok": False,
                "operation": "browser_fill_form",
                "error_type": "submit_selector_not_found",
                "selector": submit_selector,
                "filled": filled,
                "candidates": snap.get("items", [])[:15],
                "url": page.url,
            }
    settled = await _wait_until_settled(page)
    title = await page.title()
    summary = await _extract_page_text(page, max_len=1000)
    summary = f"{summary}{_loading_note(settled)}"
    return f"表单填写完成（已填 {len(filled)}/{len(field_dict)} 个字段）\n当前标题: {title}\n当前 URL: {page.url}\n内容:\n{summary}"


def _parse_browser_fill_form_fields(fields: Any) -> dict[str, Any]:
    if isinstance(fields, dict):
        return fields
    if isinstance(fields, str):
        raw = fields.strip()
        if not raw:
            raise ValueError("browser_fill_form 收到空参数")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"browser_fill_form 参数不是有效 JSON: {fields!r} ({exc.msg})"
            ) from None
        if isinstance(parsed, dict):
            return parsed
        raise ValueError(f"browser_fill_form fields 必须是 JSON 对象，实际收到: {type(parsed).__name__}")
    raise ValueError(f"browser_fill_form fields 必须是 dict 或 JSON 字符串，实际收到: {type(fields).__name__}")


async def browser_evaluate(
    url: str = "",
    script: str = "",
    timeout: int = 30,
    mode: str = "auto",
    purpose: str = "evaluate",
) -> str:
    if not script:
        return "[错误] script 不能为空"
    _, page = await _get_active_page(mode=mode, purpose=purpose or "evaluate", url=url)
    if url:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    result = await page.evaluate(script)
    result_str = json.dumps(result, ensure_ascii=False, default=str)[:2000]
    title = await page.title()
    return f"JS 执行结果:\n页面: {title}\nURL: {page.url}\n\n{result_str}"


async def browser_scroll(
    url: str = "",
    direction: str = "down",
    amount: int = 500,
    timeout: int = 30,
    mode: str = "auto",
    purpose: str = "",
) -> str:
    _, page = await _get_active_page(mode=mode, purpose=purpose, url=url)
    if url:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    delta_y = amount if direction == "down" else -amount
    await page.evaluate("dy => window.scrollBy(0, dy)", delta_y)
    settled = await _wait_until_settled(page, timeout_ms=2000)
    title = await page.title()
    summary = await _extract_page_text(page, max_len=1000)
    summary = f"{summary}{_loading_note(settled)}"
    return f"已滚动 {direction} {amount}px\n标题: {title}\n当前 URL: {page.url}\n内容:\n{summary}"


async def browser_screenshot(url: str = "", timeout: int = 30, mode: str = "auto", purpose: str = "screenshot") -> str:
    _, page = await _get_active_page(mode=mode, purpose=purpose, url=url)
    if url:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    filepath = get_app_paths().downloads_dir / f"screenshot_{timestamp}.png"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(filepath), full_page=True)
    title = await page.title()
    return f"截图已保存: {filepath}\n页面: {title}\nURL: {page.url}"


async def browser_close(mode: str = "current") -> str:
    global _current_session, _playwright_ref
    requested = (mode or "current").strip().lower()
    async with _browser_lock:
        closed = []
        session = _current_session
        should_close = requested in {"current", "active", "all"}
        if not should_close and requested in {"background", "silent", "headless", "hidden"}:
            should_close = bool(session is not None and session.visibility == "background")
        if not should_close and requested in {"foreground", "visible", "open", "headed", "front"}:
            should_close = bool(session is not None and session.visibility == "foreground")

        if should_close and session is not None:
            if await _close_session(session):
                closed.append(session.visibility)
            _current_session = None

        if requested == "all" and _playwright_ref is not None:
            try:
                await _playwright_ref.__aexit__(None, None, None)
            except Exception:
                pass
            _playwright_ref = None
    return "已关闭浏览器会话: " + (", ".join(closed) if closed else "(无)")


async def browser_current_page() -> dict[str, Any]:
    session = _current_session
    if session is None:
        return {
            "ok": False,
            "operation": "browser_current_page",
            "message": "no active browser session",
        }

    page = await _select_existing_page(session) if session.context is not None else None
    if page is not None:
        session.page = page
    if page is None:
        return {
            "ok": False,
            "operation": "browser_current_page",
            "mode": session.visibility,
            "message": "active browser session has no page",
        }
    try:
        if page.is_closed():
            return {
                "ok": False,
                "operation": "browser_current_page",
                "mode": session.visibility,
                "message": "active browser page is closed",
            }
    except Exception:
        pass
    title = await page.title()
    return {
        "ok": True,
        "operation": "browser_current_page",
        "mode": session.visibility,
        "url": page.url,
        "title": title,
    }


def _page_is_closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return False


async def _page_summary(page: Any, index: int, active_page: Any | None) -> dict[str, Any]:
    title = ""
    try:
        title = await page.title()
    except Exception:
        title = ""
    return {
        "index": index,
        "url": getattr(page, "url", ""),
        "title": title,
        "active": page is active_page,
        "closed": _page_is_closed(page),
    }


async def _list_session_pages(session: BrowserSession) -> list[dict[str, Any]]:
    pages = list(getattr(session.context, "pages", []) or [])
    active_page = session.page
    return [await _page_summary(page, index, active_page) for index, page in enumerate(pages)]


async def browser_list_pages(mode: str = "foreground") -> dict[str, Any]:
    """List pages/tabs in the attached local Playwright/CDP browser session."""
    visibility = _classify_visibility(mode=mode, purpose="local browser pages")
    try:
        session = await _ensure_attached_session(visibility)
    except BrowserDependencyError as exc:
        return {
            "ok": False,
            "operation": "browser_list_pages",
            "error_type": "cdp_unavailable",
            "message": str(exc),
            "pages": [],
            "page_count": 0,
        }
    if session.page is None:
        session.page = await _select_existing_page(session)

    page_infos = await _list_session_pages(session)
    return {
        "ok": True,
        "operation": "browser_list_pages",
        "mode": session.visibility,
        "attached": bool(session.attached),
        "page_count": len(page_infos),
        "pages": page_infos,
        "message": session.notice,
    }


async def browser_select_page(
    index: int = -1,
    url_contains: str = "",
    title_contains: str = "",
    mode: str = "foreground",
) -> dict[str, Any]:
    """Select a local browser page/tab as the active target for later browser_* calls."""
    visibility = _classify_visibility(mode=mode, purpose="select local browser page")
    try:
        session = await _ensure_attached_session(visibility)
    except BrowserDependencyError as exc:
        return {
            "ok": False,
            "operation": "browser_select_page",
            "error_type": "cdp_unavailable",
            "message": str(exc),
            "pages": [],
        }
    pages = list(getattr(session.context, "pages", []) or [])
    live_pages = [(i, page) for i, page in enumerate(pages) if not _page_is_closed(page)]
    selected: tuple[int, Any] | None = None

    try:
        wanted_index = int(index)
    except Exception:
        wanted_index = -1

    if wanted_index >= 0:
        if wanted_index < len(pages) and not _page_is_closed(pages[wanted_index]):
            selected = (wanted_index, pages[wanted_index])
    else:
        url_needle = (url_contains or "").casefold()
        title_needle = (title_contains or "").casefold()
        for page_index, page in live_pages:
            page_url = str(getattr(page, "url", ""))
            title = ""
            try:
                title = await page.title()
            except Exception:
                title = ""
            if url_needle and url_needle not in page_url.casefold():
                continue
            if title_needle and title_needle not in title.casefold():
                continue
            selected = (page_index, page)
            break
        if selected is None and not url_needle and not title_needle and live_pages:
            selected = live_pages[-1]

    if selected is None:
        return {
            "ok": False,
            "operation": "browser_select_page",
            "error_type": "page_not_found",
            "index": wanted_index,
            "url_contains": url_contains,
            "title_contains": title_contains,
            "pages": await _list_session_pages(session),
        }

    selected_index, selected_page = selected
    session.page = selected_page
    await _bring_page_to_front(selected_page)
    return {
        "ok": True,
        "operation": "browser_select_page",
        "mode": session.visibility,
        "page": await _page_summary(selected_page, selected_index, selected_page),
        "message": session.notice,
    }


async def browser_type_text(
    selector: str,
    text: str,
    url: str = "",
    clear: bool = True,
    submit: bool = False,
    submit_selector: str = "",
    timeout: int = 30,
    mode: str = "auto",
    purpose: str = "input",
) -> dict[str, Any]:
    """Type text into an element on the current or specified page without forcing navigation."""
    if not selector:
        return {
            "ok": False,
            "operation": "browser_type_text",
            "error_type": "bad_args",
            "message": "selector is required",
        }
    session, page = await _get_active_page(mode=mode, purpose=purpose or "input", url=url)
    if url:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    timeout_ms = max(1, min(int(timeout), 120)) * 1000
    try:
        await page.wait_for_selector(selector, timeout=timeout_ms)
        await page.click(selector)
        if clear:
            await page.fill(selector, "")
        await page.type(selector, text, delay=20)
        if submit_selector:
            await page.click(submit_selector)
        elif submit:
            await page.keyboard.press("Enter")
    except Exception:
        snap = await browser_observe(max_items=40, mode=mode, purpose=purpose)
        return {
            "ok": False,
            "operation": "browser_type_text",
            "error_type": "selector_not_found",
            "selector": selector,
            "candidates": snap.get("items", [])[:15],
            "url": getattr(page, "url", ""),
        }

    settled = await _wait_until_settled(page)
    title = await page.title()
    return {
        "ok": True,
        "operation": "browser_type_text",
        "mode": session.visibility,
        "selector": selector,
        "typed_chars": len(text),
        "submitted": bool(submit_selector or submit),
        "settled": settled,
        "url": getattr(page, "url", ""),
        "title": title,
        "message": session.notice,
    }


async def browser_extract_text(
    url: str = "",
    selector: str = "",
    max_chars: int = 4000,
    mode: str = "auto",
    purpose: str = "extract",
) -> dict[str, Any]:
    """Extract readable text from the current page or a selected element."""
    session, page = await _get_active_page(mode=mode, purpose=purpose, url=url)
    if url:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    max_len = max(1, min(int(max_chars), 20000))
    try:
        if selector:
            raw_text = await page.evaluate(
                """
                (selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return "";
                    return el.innerText || el.textContent || "";
                }
                """,
                selector,
            )
        else:
            raw_text = await _extract_page_text(page, max_len=max_len)
    except Exception as exc:
        return {
            "ok": False,
            "operation": "browser_extract_text",
            "error_type": "extract_failed",
            "message": str(exc),
            "selector": selector,
            "url": getattr(page, "url", ""),
        }

    text_value = str(raw_text or "")
    cleaned = _clean_text(text_value, max_len=max_len)
    title = await page.title()
    return {
        "ok": True,
        "operation": "browser_extract_text",
        "mode": session.visibility,
        "selector": selector,
        "text": cleaned,
        "truncated": len(text_value) > max_len,
        "url": getattr(page, "url", ""),
        "title": title,
        "message": session.notice,
    }


def _timeout_schema(default: int = 30) -> dict[str, Any]:
    return {"type": "integer", "minimum": 5, "maximum": 600, "default": default}


def _mode_schema(default: str = "auto") -> dict[str, Any]:
    return {
        "type": "string",
        "enum": ["auto", "background", "foreground", "silent", "visible"],
        "default": default,
        "description": "auto 自动判断；background/silent 后台静默；foreground/visible 前台持久打开",
    }


def register_playwright_tools(
    registry: ToolRegistry,
    security_service: SecurityService | None = None,
) -> None:
    """Register browser automation tools."""
    global _browser_security_service
    _browser_security_service = security_service or SecurityService()

    async def confirmed_browser_click_download(
        selector: str,
        url: str = "",
        destination_folder: str = "",
        filename: str = "",
        timeout: int = 60,
        mode: str = "background",
        purpose: str = "download",
    ) -> dict[str, Any]:
        return await browser_click_download(
            selector=selector,
            url=url,
            destination_folder=destination_folder,
            filename=filename,
            timeout=timeout,
            mode=mode,
            purpose=purpose,
            security_service=_browser_security_service,
        )

    tool_defs = [
        ToolDef(
            name="browser_open",
            description="Open URL in PawMate's persistent real Edge/CDP session; use foreground for login/video.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "mode": _mode_schema("foreground"),
                    "purpose": {"type": "string", "default": ""},
                    "timeout": _timeout_schema(),
                },
                "required": ["url"],
            },
            handler=browser_open,
        ),
        ToolDef(
            name="browser_observe",
            description="Observe current page and return actionable elements with stable selectors.",
            input_schema={
                "type": "object",
                "properties": {
                    "max_items": {"type": "integer", "minimum": 1, "maximum": 200, "default": 80},
                    "mode": _mode_schema(),
                    "purpose": {"type": "string", "default": ""},
                },
            },
            handler=browser_observe,
        ),
        ToolDef(
            name="browser_wait_for",
            description="Wait for selector/text/state on current page; returns candidates on timeout.",
            input_schema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "default": ""},
                    "text": {"type": "string", "default": ""},
                    "state": {
                        "type": "string",
                        "enum": ["attached", "detached", "visible", "hidden"],
                        "default": "visible",
                    },
                    "timeout": _timeout_schema(20),
                    "mode": _mode_schema(),
                    "purpose": {"type": "string", "default": ""},
                },
            },
            handler=browser_wait_for,
        ),
        ToolDef(
            name="browser_current_page",
            description="Return current browser page URL, title, and mode without opening a new page.",
            input_schema={"type": "object", "properties": {}},
            handler=browser_current_page,
        ),
        ToolDef(
            name="browser_list_pages",
            description="List pages in the current CDP session without launching a browser.",
            input_schema={
                "type": "object",
                "properties": {
                    "mode": _mode_schema("foreground"),
                },
            },
            handler=browser_list_pages,
        ),
        ToolDef(
            name="browser_select_page",
            description="Select an existing CDP page by index, URL fragment, or title fragment.",
            input_schema={
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "minimum": -1, "default": -1},
                    "url_contains": {"type": "string", "default": ""},
                    "title_contains": {"type": "string", "default": ""},
                    "mode": _mode_schema("foreground"),
                },
            },
            handler=browser_select_page,
        ),
        ToolDef(
            name="browser_type_text",
            description="Type text into a selector on current/URL page; can clear and submit.",
            input_schema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                    "url": {"type": "string", "default": ""},
                    "clear": {"type": "boolean", "default": True},
                    "submit": {"type": "boolean", "default": False},
                    "submit_selector": {"type": "string", "default": ""},
                    "timeout": _timeout_schema(),
                    "mode": _mode_schema(),
                    "purpose": {"type": "string", "default": "input"},
                },
                "required": ["selector", "text"],
            },
            handler=browser_type_text,
        ),
        ToolDef(
            name="browser_extract_text",
            description="Extract readable text from current/URL page or selector with max_chars limit.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "default": ""},
                    "selector": {"type": "string", "default": ""},
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 4000},
                    "mode": _mode_schema(),
                    "purpose": {"type": "string", "default": "extract"},
                },
            },
            handler=browser_extract_text,
        ),
        ToolDef(
            name="browser_navigate",
            description="Open URL through CDP and return title, text, and links.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "timeout": _timeout_schema(),
                    "mode": _mode_schema(),
                    "purpose": {"type": "string", "default": ""},
                },
                "required": ["url"],
            },
            handler=browser_navigate,
        ),
        ToolDef(
            name="browser_search",
            description="Search the web through the CDP browser and return result list.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "site": {"type": "string", "default": ""},
                    "timeout": _timeout_schema(),
                    "mode": _mode_schema("auto"),
                },
                "required": ["query"],
            },
            handler=browser_search,
        ),
        ToolDef(
            name="browser_type_and_search",
            description="Type query into a page input selector and submit.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "input_selector": {"type": "string"},
                    "text": {"type": "string"},
                    "submit_selector": {"type": "string", "default": ""},
                    "timeout": _timeout_schema(),
                    "mode": _mode_schema(),
                    "purpose": {"type": "string", "default": ""},
                },
                "required": ["url", "input_selector", "text"],
            },
            handler=browser_type_and_search,
        ),
        ToolDef(
            name="browser_click",
            description="Click selector on current/URL page; use browser_observe first.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "default": ""},
                    "selector": {"type": "string"},
                    "timeout": _timeout_schema(),
                    "mode": _mode_schema(),
                    "purpose": {"type": "string", "default": ""},
                },
                "required": ["selector"],
            },
            handler=browser_click,
        ),
        ToolDef(
            name="browser_click_download",
            description="Click selector and capture browser download event.",
            input_schema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "url": {"type": "string", "default": ""},
                    "destination_folder": {"type": "string", "default": ""},
                    "filename": {"type": "string", "default": ""},
                    "timeout": _timeout_schema(60),
                    "mode": _mode_schema("background"),
                    "purpose": {"type": "string", "default": "download"},
                },
                "required": ["selector"],
            },
            handler=confirmed_browser_click_download,
        ),
        ToolDef(
            name="browser_fill_form",
            description="Fill form fields and optionally submit; confirmation required.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "fields": {"type": "string"},
                    "submit_selector": {"type": "string", "default": ""},
                    "timeout": _timeout_schema(),
                    "mode": _mode_schema("foreground"),
                    "purpose": {"type": "string", "default": "form"},
                },
                "required": ["url", "fields"],
            },
            handler=browser_fill_form,
            approval=APPROVAL_CONFIRM,
        ),
        ToolDef(
            name="browser_evaluate",
            description="Run JavaScript on current/URL page and return JSON.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "default": ""},
                    "script": {"type": "string"},
                    "timeout": _timeout_schema(),
                    "mode": _mode_schema(),
                    "purpose": {"type": "string", "default": "evaluate"},
                },
                "required": ["script"],
            },
            handler=browser_evaluate,
            approval=APPROVAL_CONFIRM,
        ),
        ToolDef(
            name="browser_scroll",
            description="Scroll current/URL page up or down by amount.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "default": ""},
                    "direction": {"type": "string", "enum": ["down", "up"], "default": "down"},
                    "amount": {"type": "integer", "default": 500},
                    "timeout": _timeout_schema(),
                    "mode": _mode_schema(),
                    "purpose": {"type": "string", "default": ""},
                },
            },
            handler=browser_scroll,
        ),
        ToolDef(
            name="browser_screenshot",
            description="Save page.screenshot() for current/URL page to data/downloads.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "default": ""},
                    "timeout": _timeout_schema(),
                    "mode": _mode_schema("auto"),
                    "purpose": {"type": "string", "default": "screenshot"},
                },
            },
            handler=browser_screenshot,
        ),
        ToolDef(
            name="browser_close",
            description="Close current, foreground, background, or all browser sessions.",
            input_schema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["current", "background", "foreground", "all"],
                        "default": "current",
                    },
                },
            },
            handler=browser_close,
        ),
    ]
    for tool_def in tool_defs:
        registry.register(tool_def)
    logger.info("[Browser] registered %d browser tools", len(tool_defs))

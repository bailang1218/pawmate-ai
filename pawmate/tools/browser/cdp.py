"""Pure CDP attach helpers for the user's real Edge profile.

This module only probes or attaches to a browser that was started with a
debugging port. It never falls back to an empty managed profile.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


DEFAULT_CDP_URL = "http://127.0.0.1:9222"


class NativeCdpUnavailable(RuntimeError):
    """No debuggable native browser is available."""


_HINT = (
    "未找到可接管的调试浏览器。请用「Edge (调试模式)」快捷方式启动你的 Edge"
    "（或先运行 setup_debug_edge.ps1 完成一次性设置），再重试。"
    "PawMate 不会改用没有登录态的空浏览器。"
)


def _version_url(cdp_url: str) -> str:
    return cdp_url.rstrip("/") + "/json/version"


def _list_url(cdp_url: str) -> str:
    return cdp_url.rstrip("/") + "/json/list"


def probe_cdp(cdp_url: str = DEFAULT_CDP_URL, timeout: float = 1.5) -> Optional[dict]:
    """Return /json/version as a dict when the CDP endpoint is alive."""
    try:
        with urllib.request.urlopen(_version_url(cdp_url), timeout=timeout) as resp:
            if getattr(resp, "status", 200) == 200:
                return json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:
        return None
    return None


def probe_cdp_targets(cdp_url: str = DEFAULT_CDP_URL, timeout: float = 1.5) -> Optional[list[dict[str, Any]]]:
    """Return /json/list as a list when the CDP endpoint is alive."""
    try:
        with urllib.request.urlopen(_list_url(cdp_url), timeout=timeout) as resp:
            if getattr(resp, "status", 200) == 200:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
                return data if isinstance(data, list) else []
    except Exception:
        return None
    return None


def _cdp_port(cdp_url: str) -> int:
    return urlparse(cdp_url).port or 9222


def any_edge_running() -> bool:
    """Return whether msedge.exe is already running."""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.lower()
        return "msedge.exe" in out
    except Exception:
        return False


def _edge_executable() -> Optional[str]:
    if sys.platform != "win32":
        return None
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if not base:
            continue
        path = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if path.exists():
            return str(path)
    return None


def launch_debug_edge_real_profile(port: int) -> bool:
    """Launch Edge with a debugging port and the real default profile.

    This is only meaningful when no Edge process is already running. If a
    normal Edge has the profile locked, Chromium's single-instance handoff will
    prevent the debugging port from opening.
    """
    exe = _edge_executable()
    if not exe:
        return False
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [exe, f"--remote-debugging-port={port}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
        return True
    except Exception:
        return False


def _poll_probe(cdp_url: str, wait_s: float) -> Optional[dict]:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        info = probe_cdp(cdp_url)
        if info is not None:
            return info
        time.sleep(0.4)
    return None


async def attach_native_edge(
    pw: Any,
    *,
    cdp_url: str = DEFAULT_CDP_URL,
    auto_launch: bool = True,
    launch_wait_s: float = 12.0,
):
    """Pure attach to the user's real Edge. Return (browser, context)."""
    info = await asyncio.to_thread(probe_cdp, cdp_url)

    if info is None and auto_launch and not any_edge_running():
        if await asyncio.to_thread(launch_debug_edge_real_profile, _cdp_port(cdp_url)):
            info = await asyncio.to_thread(_poll_probe, cdp_url, launch_wait_s)

    if info is None:
        raise NativeCdpUnavailable(_HINT)

    browser = await pw.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    return browser, context

"""Preflight helpers for taking over a native Edge profile.

The module is intentionally split into pure decision helpers and side-effecting
helpers. Unit tests should exercise the pure layer; the Windows process-closing
path needs local manual verification with a real Edge session.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

try:
    import psutil
except ImportError:  # pragma: no cover - exercised by environments without psutil.
    psutil = None

logger = logging.getLogger("pawmate")

_EDGE_PROC_NAMES = {"msedge.exe", "msedge"}

if os.name == "nt":
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
else:
    _NO_WINDOW = 0


@dataclass(frozen=True)
class RawProc:
    """Detached process snapshot for pure tests."""

    pid: int
    name: str
    cmdline: list[str] = field(default_factory=list)


def _arg_value(cmdline: list[str], flag: str) -> Optional[str]:
    """Return a command-line flag value, supporting both --flag=value and --flag value."""

    for i, arg in enumerate(cmdline):
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1].strip('"')
        if arg == flag and i + 1 < len(cmdline):
            return cmdline[i + 1].strip('"')
    return None


def _norm(path: Optional[str]) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).resolve()).lower()
    except Exception:
        return str(path).strip().lower()


@dataclass
class NativeEdgeState:
    running: bool
    on_native_profile: bool
    has_debug_port: bool
    browser_pids: list[int]


def infer_native_state(
    procs: list[RawProc],
    user_data_root: Optional[Path],
    port: int,
) -> NativeEdgeState:
    """Infer whether the real Edge profile is currently occupied.

    Edge launched from the Start menu commonly has no --user-data-dir flag.
    That main process must be treated as occupying the default native profile.
    """

    native_norm = _norm(str(user_data_root)) if user_data_root else ""
    pids: list[int] = []
    on_native = False
    has_debug = False
    for proc in procs:
        if (proc.name or "").lower() not in _EDGE_PROC_NAMES:
            continue
        if any(arg.startswith("--type=") for arg in proc.cmdline):
            continue
        pids.append(proc.pid)
        user_data_dir = _arg_value(proc.cmdline, "--user-data-dir")
        if not user_data_dir or (native_norm and _norm(user_data_dir) == native_norm):
            on_native = True
        if _arg_value(proc.cmdline, "--remote-debugging-port") == str(port):
            has_debug = True
    return NativeEdgeState(
        running=bool(pids),
        on_native_profile=on_native,
        has_debug_port=has_debug,
        browser_pids=pids,
    )


def needs_graceful_restart(
    state: NativeEdgeState,
    *,
    cdp_alive: bool,
    want_native: bool,
) -> bool:
    """Return whether PawMate should ask to close and relaunch native Edge."""

    return (
        want_native
        and not cdp_alive
        and state.running
        and state.on_native_profile
        and not state.has_debug_port
    )


def _enumerate_edge_procs() -> list[RawProc]:
    """Convert psutil process rows into detached RawProc values."""

    if psutil is None:
        return []
    out: list[RawProc] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            out.append(
                RawProc(
                    pid=proc.info["pid"],
                    name=proc.info["name"] or "",
                    cmdline=list(proc.info["cmdline"] or []),
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def _probe_cdp_sync(port: int, timeout: float) -> Optional[dict]:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version",
            timeout=timeout,
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


async def probe_cdp(port: int = 9222, timeout: float = 1.0) -> Optional[dict]:
    return await asyncio.to_thread(_probe_cdp_sync, port, timeout)


def inspect_native_edge(user_data_root: Optional[Path], port: int) -> NativeEdgeState:
    """Enumerate Edge and infer profile occupancy.

    Without psutil we conservatively report possible native occupancy so the
    upper layer can ask for confirmation instead of silently assuming safety.
    """

    if psutil is None:
        return NativeEdgeState(
            running=True,
            on_native_profile=True,
            has_debug_port=False,
            browser_pids=[],
        )
    return infer_native_state(_enumerate_edge_procs(), user_data_root, port)


@dataclass
class CloseResult:
    closed: bool
    forced: bool


def _has_browser_proc() -> bool:
    return any(
        (proc.name or "").lower() in _EDGE_PROC_NAMES
        and not any(arg.startswith("--type=") for arg in proc.cmdline)
        for proc in _enumerate_edge_procs()
    )


async def graceful_close_edge(timeout: float = 8.0) -> CloseResult:
    """Close Edge gracefully first; force only after a timeout."""

    if os.name != "nt":
        raise RuntimeError("graceful_close_edge is only supported on Windows")
    subprocess.run(
        ["taskkill", "/IM", "msedge.exe"],
        capture_output=True,
        creationflags=_NO_WINDOW,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _has_browser_proc():
            logger.info("[BrowserPreflight] Edge closed gracefully; session should be saved")
            return CloseResult(closed=True, forced=False)
        await asyncio.sleep(0.3)

    logger.warning(
        "[BrowserPreflight] Graceful Edge close timed out; refusing to force-kill user browser processes"
    )
    return CloseResult(closed=False, forced=False)


async def ensure_native_profile_unlocked(
    user_data_root: Optional[Path],
    *,
    port: int = 9222,
    confirm_restart: Optional[Callable[[], Awaitable[bool]]] = None,
) -> CloseResult | None:
    """Close native Edge when it is blocking a requested native takeover.

    None means no close was needed, CDP is already available, or the user did
    not approve the restart. A CloseResult means Edge was closed or attempted.
    """

    version = await probe_cdp(port)
    if version:
        return None
    state = await asyncio.to_thread(inspect_native_edge, user_data_root, port)
    if not needs_graceful_restart(state, cdp_alive=False, want_native=True):
        return None
    if confirm_restart is not None and not await confirm_restart():
        logger.info("[BrowserPreflight] User declined native Edge restart")
        return CloseResult(closed=False, forced=False)
    return await graceful_close_edge(timeout=8.0)

"""Shell command boundary checks for local execution tools."""
from __future__ import annotations

import re
from dataclasses import dataclass


class ShellBoundaryError(Exception):
    """Raised when a shell command is outside the runtime boundary."""


MAX_SHELL_TIMEOUT_SECONDS = 60
BACKGROUND_PATTERNS = (
    r"(^|\s)nohup\s+",
    r"(^|\s)setsid\s+",
    r"(^|\s)start\s+/b\s+",
    r"(^|\s)Start-Job\s+",
    r"(^|[\s'\"])Start-Process\s+",
    r"\s&\s*$",
)
_START_PROCESS_PATTERN = re.compile(r"(^|[\s'\"])Start-Process\s+", re.IGNORECASE)
_START_PROCESS_TARGET_PATTERN = re.compile(
    r"(?:^|[\s'\"])Start-Process\s+(?:-FilePath\s+)?(?P<target>\"[^\"]+\"|'[^']+'|[^\s]+)",
    re.IGNORECASE,
)
_SHELL_CHAIN_PATTERN = re.compile(r"(?:\r|\n|;|&&|\|\||(?<!\|)\|(?!\|))")
_UNSAFE_BROWSER_LAUNCH_TAIL_PATTERN = re.compile(r"[`$&|;<>{}\r\n()]")
_allow_browser_process_launch = False
DANGEROUS_PATTERNS = (
    r"\brm\s+-rf\b",
    r"\bRemove-Item\b.*\b-Recurse\b",
    r"\brmdir\s+/s\b",
    r"\bdel\s+/[fsq]",
    r"\breg\s+delete\b",
    r"\bsc\s+delete\b",
    r"\bnet\s+user\b",
    r"\bchmod\s+777\b",
    r"\bchown\b",
)


@dataclass(frozen=True)
class ShellCommandBoundary:
    command: str
    timeout: int
    risk: str
    background: bool
    dangerous: bool


def configure_shell_boundary(*, allow_browser_process_launch: bool = False) -> None:
    """Apply the app-level manual override for browser process launches."""
    global _allow_browser_process_launch
    _allow_browser_process_launch = bool(allow_browser_process_launch)


def browser_process_launch_allowed() -> bool:
    return _allow_browser_process_launch


def _is_single_browser_start_process(command: str) -> bool:
    """Allow one PowerShell Start-Process call targeting a known browser only."""
    if _SHELL_CHAIN_PATTERN.search(command):
        return False
    if len(_START_PROCESS_PATTERN.findall(command)) != 1:
        return False
    match = _START_PROCESS_TARGET_PATTERN.search(command)
    if match is None or match.start() != 0:
        return False
    target = match.group("target").strip("'\"").replace("/", "\\")
    executable = target.rsplit("\\", 1)[-1].lower()
    if executable not in {
        "msedge", "msedge.exe", "chrome", "chrome.exe",
        "chromium", "chromium.exe", "firefox", "firefox.exe",
    }:
        return False
    return not _UNSAFE_BROWSER_LAUNCH_TAIL_PATTERN.search(command[match.end():])


def clamp_shell_timeout(timeout: int | None) -> int:
    try:
        value = int(timeout or 20)
    except (TypeError, ValueError):
        value = 20
    return max(1, min(value, MAX_SHELL_TIMEOUT_SECONDS))


def analyze_shell_command(command: str, timeout: int | None = None) -> ShellCommandBoundary:
    cmd = str(command or "").strip()
    if not cmd:
        raise ShellBoundaryError("shell command is empty")
    background = any(re.search(pattern, cmd, re.IGNORECASE) for pattern in BACKGROUND_PATTERNS)
    dangerous = any(re.search(pattern, cmd, re.IGNORECASE) for pattern in DANGEROUS_PATTERNS)
    browser_launch_override = (
        background
        and _allow_browser_process_launch
        and _is_single_browser_start_process(cmd)
    )
    if background and not browser_launch_override:
        raise ShellBoundaryError("background or detached shell commands are not allowed")
    if dangerous:
        raise ShellBoundaryError("dangerous or destructive shell commands are not allowed")
    risk = "critical" if dangerous else "high"
    return ShellCommandBoundary(
        command=cmd,
        timeout=clamp_shell_timeout(timeout),
        risk=risk,
        background=background,
        dangerous=dangerous,
    )

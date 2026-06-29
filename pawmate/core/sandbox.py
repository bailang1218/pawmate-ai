"""
Sandbox mode configuration and command permission check.

Safe / Dev / Full / Off four-level model with hard safety guard.
"""
from __future__ import annotations

import logging
from typing import Tuple

_logger = logging.getLogger("pawmate")

VALID_SANDBOX_MODES = ("safe", "dev", "full", "off")

_HARD_BLOCKED_PATTERNS = [
    "rm -rf /", "rm -rf /*", "sudo rm -rf /", "sudo rm -rf /*",
    "del /s /q c:", "del /s /q c:\\", 'del /s /q "c:\\',
    "rmdir /s /q c:", "rmdir /s /q c:\\",
    "format ", "mkfs", "diskpart",
    "shutdown", "reboot",
    "reg delete", "bcdedit",
    "takeown /f c:\\windows", "takeown /f c:\\",
]

_HARD_BLOCKED_COMMANDS = {"format", "mkfs", "diskpart", "shutdown", "reboot"}


def is_hard_blocked_command(command: str) -> bool:
    """Check if a command is permanently blocked regardless of sandbox mode.

    These commands are extreme destructive operations that should NEVER
    be allowed, even when sandbox is fully disabled.
    """
    stripped = command.strip().lower()
    if not stripped:
        return False

    for pattern in _HARD_BLOCKED_PATTERNS:
        if pattern in stripped:
            return True

    first_word = stripped.split()[0] if stripped.split() else ""
    if first_word in _HARD_BLOCKED_COMMANDS:
        return True

    if "rm -rf" in stripped:
        for target in [" /", " /*", " /root", " /home", " /etc", " /usr", " /bin", " /boot", " /dev"]:
            if target in stripped:
                return True
    if "rd /s /q" in stripped or "rmdir /s" in stripped:
        for target in ["c:", "d:", "\\", "/"]:
            if target in stripped:
                return True

    return False


def resolve_sandbox_mode(config: dict) -> str:
    """Resolve sandbox_mode from config, default to 'safe'."""
    sec = config.get("security", {})
    raw = sec.get("command_sandbox_mode") or sec.get("sandbox_mode", "safe")
    mode = str(raw).strip().lower()
    if mode in VALID_SANDBOX_MODES:
        return mode
    _logger.warning("[Sandbox] invalid sandbox_mode=%r, falling back to 'safe'", raw)
    return "safe"


def is_command_allowed(command: str, sandbox_mode: str) -> Tuple[bool, str]:
    """Check if a command is allowed in the given sandbox mode.

    1. Hard safety guard — all modes, permanent.
    2. Mode-specific check.

    Returns (allowed, reason).
    """
    stripped = command.strip().lower()
    if not stripped:
        return False, "命令为空"

    # ── 1. Hard safety guard — all modes, permanent ──────────
    if is_hard_blocked_command(stripped):
        return (
            False,
            "该命令属于极端破坏性操作，已被永久安全保护拦截。此保护无法通过关闭沙箱绕过。",
        )

    sandbox_mode = str(sandbox_mode).strip().lower()

    # ── 2. Mode-specific checks ───────────────────────────────
    if sandbox_mode == "off":
        return True, "sandbox disabled"

    if sandbox_mode == "full":
        return True, ""

    if sandbox_mode == "dev":
        ALLOWED_DEV_COMMANDS = [
            "pip install",
            "python -m pip",
            "python -m pytest",
            "python -m playwright install",
            "playwright install",
            "npm install",
            "npm run",
        ]
        for allowed in ALLOWED_DEV_COMMANDS:
            if stripped.startswith(allowed):
                return True, ""
        BLOCKED_DEV_KEYWORDS = [
            "sudo ", "su ", "chmod ", "chown ", "passwd ",
            "del /f", "rmdir /s", "reg ", "sc ",
            "net user", "net localgroup",
        ]
        for keyword in BLOCKED_DEV_KEYWORDS:
            if keyword in stripped:
                return False, f"开发模式不允许此操作: {keyword}"
        return True, ""

    # safe mode
    ALLOWED_SAFE_COMMANDS = [
        "dir", "ls", "cat", "type", "echo", "cd ", "pwd",
        "python --version", "python -v", "where ", "which ",
        "git status", "git log", "git diff", "git branch",
        "pip list", "pip show", "pip freeze",
        "python -m pip list",
    ]
    for allowed in ALLOWED_SAFE_COMMANDS:
        if stripped.startswith(allowed):
            return True, ""

    return False, f"当前 sandbox_mode={sandbox_mode}，已阻止该操作: {stripped[:60]}"

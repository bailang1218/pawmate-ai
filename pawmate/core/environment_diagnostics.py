"""Startup environment diagnostics for browser, network, and coding tools.

The checks here are intentionally lightweight and non-blocking in spirit:
startup should keep going even if one diagnostic fails. Results are written to
the PawMate log so users can inspect the environment from the Logs panel.
"""
from __future__ import annotations

import importlib.util
import locale
import logging
import shutil
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pawmate.core.browser_diagnostics import check_browser_dependencies
from pawmate.core.llm_router import get_available_llm_providers

logger = logging.getLogger("pawmate")


@dataclass(frozen=True)
class DiagnosticItem:
    name: str
    status: str
    message: str
    detail: str = ""
    hint: str = ""


@dataclass(frozen=True)
class StartupEnvironmentReport:
    items: list[DiagnosticItem] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for item in self.items if item.status == "ok")

    @property
    def warning_count(self) -> int:
        return sum(1 for item in self.items if item.status == "warning")

    @property
    def error_count(self) -> int:
        return sum(1 for item in self.items if item.status == "error")

    @property
    def summary(self) -> str:
        return f"{self.ok_count} ok, {self.warning_count} warning, {self.error_count} error"


_PROVIDER_HOSTS = {
    "anthropic": "api.anthropic.com",
    "deepseek": "api.deepseek.com",
    "gemini": "generativelanguage.googleapis.com",
    "minimaxi": "api.minimax.chat",
    "openai": "api.openai.com",
    "qwen": "dashscope.aliyuncs.com",
}


def run_startup_environment_diagnostics(
    config_data: dict[str, Any] | None = None,
    *,
    network_timeout: float = 1.5,
) -> StartupEnvironmentReport:
    """Run startup diagnostics and return a structured report."""
    config = config_data if isinstance(config_data, dict) else {}
    items: list[DiagnosticItem] = []
    items.append(_check_system_browser())
    items.append(_check_playwright_browser())
    items.extend(_check_network(config, timeout=network_timeout))
    items.extend(_check_coding_environment())
    return StartupEnvironmentReport(items)


def log_startup_environment_diagnostics(config_data: dict[str, Any] | None = None) -> StartupEnvironmentReport:
    """Run diagnostics, log every item, and never raise to the caller."""
    try:
        report = run_startup_environment_diagnostics(config_data)
    except Exception as exc:
        logger.warning("[StartupDiagnostics] environment diagnostics failed: %s", exc)
        return StartupEnvironmentReport([
            DiagnosticItem(
                name="startup_diagnostics",
                status="warning",
                message="启动环境检测自身失败，但不会阻止 PawMate 启动。",
                detail=str(exc),
            )
        ])

    logger.info("[StartupDiagnostics] %s", report.summary)
    for item in report.items:
        log = logger.info if item.status == "ok" else logger.warning
        suffix = f" | {item.detail}" if item.detail else ""
        hint = f" | hint: {item.hint}" if item.hint else ""
        log("[StartupDiagnostics] %s: %s%s%s", item.name, item.message, suffix, hint)
    return report


def _check_system_browser() -> DiagnosticItem:
    try:
        from pawmate.tools.playwright_browser import _windows_default_browser_executable

        executable = _windows_default_browser_executable()
    except Exception as exc:
        return DiagnosticItem(
            name="system_browser",
            status="warning",
            message="没有成功读取系统默认浏览器。",
            detail=str(exc),
            hint="前台浏览器任务会继续使用配置浏览器或内置 Chromium 兜底。",
        )

    if executable:
        label = Path(executable).name
        return DiagnosticItem(
            name="system_browser",
            status="ok",
            message=f"检测到可控的系统默认浏览器：{label}",
            detail=executable,
        )
    return DiagnosticItem(
        name="system_browser",
        status="warning",
        message="没有检测到可由 Playwright 控制的系统默认 Chromium 浏览器。",
        hint="如果你用 Edge/Chrome 作为默认浏览器，前台浏览器任务会优先尝试它；否则会自动回退到内置 Chromium。",
    )


def _check_playwright_browser() -> DiagnosticItem:
    status = check_browser_dependencies()
    if status.available:
        return DiagnosticItem(
            name="playwright_browser",
            status="ok",
            message="内置浏览器自动化依赖可用。",
            detail=status.chromium_executable_path or status.browsers_path,
        )
    return DiagnosticItem(
        name="playwright_browser",
        status="warning",
        message=status.message,
        detail=status.detail or status.browsers_path,
        hint=status.install_hint,
    )


def _check_network(config_data: dict[str, Any], *, timeout: float) -> list[DiagnosticItem]:
    hosts = _provider_hosts_for_config(config_data)
    if not hosts:
        hosts = {"network_baseline": "www.baidu.com"}
    items: list[DiagnosticItem] = []
    for label, host in hosts.items():
        items.append(_check_tcp_host(f"network_{label}", host, 443, timeout=timeout))
    return items


def _provider_hosts_for_config(config_data: dict[str, Any]) -> dict[str, str]:
    hosts: dict[str, str] = {}
    try:
        available = get_available_llm_providers(config_data)
    except Exception:
        available = set()
    llm_cfg = config_data.get("llm", {}) if isinstance(config_data, dict) else {}
    for provider in sorted(available):
        provider_cfg = llm_cfg.get(provider, {}) if isinstance(llm_cfg, dict) else {}
        base_url = provider_cfg.get("base_url", "") if isinstance(provider_cfg, dict) else ""
        host = _host_from_base_url(base_url) or _PROVIDER_HOSTS.get(provider, "")
        if host:
            hosts[provider] = host
    return hosts


def _host_from_base_url(base_url: str) -> str:
    value = str(base_url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return parsed.hostname or ""


def _check_tcp_host(name: str, host: str, port: int, *, timeout: float) -> DiagnosticItem:
    try:
        socket.getaddrinfo(host, port)
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return DiagnosticItem(
            name=name,
            status="ok",
            message=f"网络可连通：{host}:{port}",
        )
    except Exception as exc:
        return DiagnosticItem(
            name=name,
            status="warning",
            message=f"网络连通性检测失败：{host}:{port}",
            detail=type(exc).__name__ + ": " + str(exc),
            hint="如果模型调用失败，先检查代理、DNS、防火墙或当前网络。",
        )


def _check_coding_environment() -> list[DiagnosticItem]:
    items = [
        DiagnosticItem(
            name="python",
            status="ok",
            message=f"Python 可用：{sys.version.split()[0]}",
            detail=sys.executable,
        ),
        _command_item("node", "Node.js", "前端脚本检查会跳过或失败。"),
        _command_item("git", "Git", "提交、版本回滚和差异查看会受影响。"),
        _command_item("powershell", "PowerShell", "Windows 命令执行能力会受影响。"),
        _pytest_item(),
        _encoding_item(),
    ]
    return items


def _command_item(command: str, label: str, missing_hint: str) -> DiagnosticItem:
    path = shutil.which(command)
    if path:
        return DiagnosticItem(
            name=command,
            status="ok",
            message=f"{label} 可用。",
            detail=path,
        )
    return DiagnosticItem(
        name=command,
        status="warning",
        message=f"未在 PATH 中找到 {label}。",
        hint=missing_hint,
    )


def _pytest_item() -> DiagnosticItem:
    if importlib.util.find_spec("pytest") is not None:
        return DiagnosticItem(
            name="pytest",
            status="ok",
            message="pytest 可用。",
        )
    return DiagnosticItem(
        name="pytest",
        status="warning",
        message="pytest 不可用。",
        hint="无法运行自动化测试；可用当前 Python 安装 pytest。",
    )


def _encoding_item() -> DiagnosticItem:
    preferred = locale.getpreferredencoding(False)
    filesystem = sys.getfilesystemencoding()
    detail = f"preferred={preferred}, filesystem={filesystem}"
    if "utf" in preferred.lower() and "utf" in filesystem.lower():
        return DiagnosticItem(
            name="encoding",
            status="ok",
            message="系统编码环境偏 UTF-8。",
            detail=detail,
        )
    return DiagnosticItem(
        name="encoding",
        status="warning",
        message="系统默认编码不是纯 UTF-8，读写文件时需要显式指定编码。",
        detail=detail,
        hint="PawMate 会尽量使用 UTF-8/replace 读写；遇到乱码时优先检查文件真实编码。",
    )

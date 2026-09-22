"""内置网关工具：在 MCP 不可用时直接提供可调用能力。"""
from __future__ import annotations

import asyncio
import ast
import logging
import shlex
import os
import platform
import re
import subprocess
import shutil
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional

from pawmate.tools.core.registry import (
    ToolDef,
    ToolRegistry,
    APPROVAL_AUTO,
    APPROVAL_CONFIRM,
    APPROVAL_NOTIFY,
    RiskLevel,
    SideEffectLevel,
    ToolCategory,
)
from pawmate.core.safety.file_boundary import FileBoundaryError, assert_text_file_readable
from pawmate.core.safety.network_boundary import NetworkBoundaryError, validate_external_http_url
from pawmate.core.safety.path_security import PathSecurityError
from pawmate.core.safety.security_service import SecurityService
from pawmate.core.safety.shell_boundary import ShellBoundaryError, analyze_shell_command
from pawmate.tools.security.sandbox import Sandbox, SecurityViolation
from pawmate.tools.vision.stub import capture_screenshot, ocr_image, vision_analyze
from pawmate.tools.downloads.download_tool import register_download_tools
from pawmate.tools.browser.register import register_browser_tools
from pawmate.tools.web_search import register_native_web_search_tools
from pawmate.core.observability.browser_diagnostics import log_browser_diagnostics
from pawmate.core.safety.resource_limits import MAX_TEXT_READ_BYTES, read_text_limited


_sandbox = Sandbox(max_runtime=60, max_output_size=1024 * 1024)
_security_service = SecurityService()
_IS_WINDOWS = platform.system() == "Windows"
_operation_logger = logging.getLogger("pawmate.operation")


_READONLY_POWERSHELL_DENY = re.compile(
    r"(?ix)"
    r"(\b(set|new|remove|rename|move|copy|clear|start|stop|restart|suspend|resume|"
    r"invoke|install|uninstall|enable|disable|register|unregister|add)-[a-z0-9_-]+\b|"
    r"\b(out-file|set-content|add-content|export-[a-z0-9_-]+|tee-object)\b|"
    r"\b(iwr|irm|curl|wget|del|erase|rd|rmdir|mkdir|md|copy|xcopy|robocopy|move|"
    r"ren|reg\s+(add|delete|import|restore|save|copy)|schtasks\s+/(create|delete|change)|"
    r"sc\s+(delete|create|config|start|stop)|net\s+user|net\s+localgroup)\b|"
    r"(?<![<>=])>(?![=>]))"
)
_READONLY_POWERSHELL_ALLOW = re.compile(
    r"(?ix)"
    r"(^|\b)(get|select|where|sort|format|measure|group|compare|convertto)-[a-z0-9_-]+\b|"
    r"\b(gci|ls|dir|gc|type|cat|gps|ps|gsv|gwmi|gcim|gi|gp|rvpa|pwd|whoami|hostname|"
    r"systeminfo|tasklist|driverquery|ipconfig|wmic|query|netstat)\b|"
    r"\b(netsh\s+.+\s+show)\b|"
    r"\b(test-path)\b"
)


def _format_result(result: dict) -> str:
    lines: List[str] = []
    if result.get("stdout"):
        lines.append(str(result["stdout"]))
    if result.get("stderr"):
        lines.append(f"[stderr] {result['stderr']}")
    if not lines:
        lines.append("(no output)")
    if not result.get("ok", False):
        lines.append(f"[exit_code {result.get('rc', -1)}]")
    return "\n".join(lines)


def _safe_path(path: str, *, mode: str = "read") -> str:
    if not path:
        return str(Path.home())
    try:
        return _security_service.check_path(path, mode=mode)
    except (PathSecurityError, FileBoundaryError) as exc:
        raise SecurityViolation(str(exc)) from exc


def _strip_wrapping_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
        return v[1:-1]
    return v


def _ps_single_quote(value: str) -> str:
    # PowerShell single-quoted string escape: ''
    return "'" + value.replace("'", "''") + "'"


def _normalize_command_for_platform(command: str) -> str:
    """Normalize common open-url commands across platforms."""
    cmd = command.strip()
    if not cmd:
        return command

    if _IS_WINDOWS:
        lowered = cmd.lower()

        if lowered.startswith("xdg-open "):
            target = _strip_wrapping_quotes(cmd[len("xdg-open "):])
            if target:
                return f"Start-Process {_ps_single_quote(target)}"

        if lowered.startswith("open "):
            target = _strip_wrapping_quotes(cmd[len("open "):])
            if target:
                return f"Start-Process {_ps_single_quote(target)}"

    return command


def _normalize_url(url: str) -> str:
    target = _strip_wrapping_quotes(url).strip()
    if not target:
        return ""
    lowered = target.lower()
    if lowered.startswith(("http://", "https://", "file://", "mailto:")):
        return target
    if target.startswith("www."):
        return f"https://{target}"
    return target


def _split_app_args(args: str) -> List[str]:
    if not args.strip():
        return []
    try:
        return shlex.split(args, posix=not _IS_WINDOWS)
    except Exception:
        return [args]


def _resolve_vscode_launcher() -> str:
    for candidate in ("code", "code.cmd", "code.CMD"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved

    common_locations = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft VS Code" / "bin" / "code.cmd",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft VS Code" / "bin" / "code.cmd",
    ]
    for candidate in common_locations:
        if candidate and candidate.exists():
            return str(candidate)

    return "code"


def _maybe_launch_vscode(command: str, cwd: str = "") -> Optional[str]:
    cmd = command.strip()
    if not cmd:
        return None

    try:
        parts = shlex.split(cmd, posix=not _IS_WINDOWS)
    except Exception:
        return None

    if not parts:
        return None

    if parts[0].lower() != "code":
        return None

    launcher = _resolve_vscode_launcher()
    args = " ".join(parts[1:])
    return f"{launcher} {args}".strip() if args else launcher


async def run_shell_command(command: str, cwd: str = "", timeout: int = 20) -> str:
    from pawmate.tools.text.encoding import validate_shell_payload

    try:
        boundary = analyze_shell_command(command, timeout)
    except ShellBoundaryError as e:
        return f"[security] {e}"

    # Field-level encoding diagnostics
    encoding_check = validate_shell_payload(
        command=command,
        cwd=cwd or None,
        encoding="gbk",
    )
    if encoding_check is not None:
        import json
        return json.dumps(encoding_check, ensure_ascii=False)

    vscode_command = _maybe_launch_vscode(command, cwd=cwd)
    normalized_command = vscode_command or _normalize_command_for_platform(command)
    safe_cwd = _safe_path(cwd, mode="execute") if cwd.strip() else None
    result = await _sandbox.execute_shell_command(
        cmd=normalized_command,
        cwd=safe_cwd,
        timeout=boundary.timeout,
    )
    return _format_result(result)


# MCP 兼容别名
async def run_command(command: str, cwd: str = "", timeout: int = 20) -> str:
    return await run_shell_command(command=command, cwd=cwd, timeout=timeout)


def _validate_readonly_powershell_query(command: str) -> tuple[bool, str]:
    cmd = str(command or "").strip()
    if not cmd:
        return False, "empty command"
    if _READONLY_POWERSHELL_DENY.search(cmd):
        return False, "command contains write/process/network mutation verbs"
    if not _READONLY_POWERSHELL_ALLOW.search(cmd):
        return False, "command is not recognized as a read-only PowerShell query"
    return True, ""


async def run_powershell_query(command: str, cwd: str = "", timeout: int = 20) -> str:
    """Run a read-only PowerShell/system-inspection query without UI confirmation."""
    if not _IS_WINDOWS:
        return "[unsupported] run_powershell_query is only available on Windows"

    ok, reason = _validate_readonly_powershell_query(command)
    if not ok:
        return f"[security] read-only PowerShell query denied: {reason}"

    try:
        boundary = analyze_shell_command(command, timeout)
    except ShellBoundaryError as e:
        return f"[security] {e}"

    safe_cwd = _safe_path(cwd, mode="execute") if cwd.strip() else None
    result = await _sandbox.execute_shell_command(
        cmd=command.strip(),
        cwd=safe_cwd,
        timeout=boundary.timeout,
    )
    _operation_logger.info(
        "[PowerShellQuery] ok=%s rc=%s timeout=%ss cwd=%s command=%s",
        bool(result.get("ok", False)),
        result.get("rc", -1),
        boundary.timeout,
        safe_cwd or "",
        command.strip()[:500],
    )
    return _format_result(result)


async def open_url(url: str) -> str:
    target = _normalize_url(url)
    if not target:
        return "[error] empty url"
    try:
        target = validate_external_http_url(target)
    except NetworkBoundaryError as e:
        return f"[security] {e}"

    try:
        opened = await asyncio.to_thread(webbrowser.open, target, 2, True)
        if opened:
            return f"opened: {target}"
    except Exception:
        # Fallback to shell path below.
        pass

    if _IS_WINDOWS:
        cmd = _normalize_command_for_platform(f"open {target}")
    elif platform.system() == "Darwin":
        cmd = f'open "{target}"'
    else:
        cmd = f'xdg-open "{target}"'

    result = await _sandbox.execute_shell_command(cmd=cmd, timeout=20)
    if result.get("ok", False):
        return f"opened: {target}"
    return _format_result(result)


async def launch_desktop_app(target: str, args: str = "") -> str:
    app_target = (target or "").strip()
    if not app_target:
        return "[error] empty target"

    app_args = _split_app_args(args)

    try:
        app_path = Path(app_target).expanduser()
        if app_path.exists():
            safe_app_path = _safe_path(str(app_path), mode="execute")
            if _IS_WINDOWS:
                os.startfile(safe_app_path)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", safe_app_path, *app_args])
            else:
                subprocess.Popen([safe_app_path, *app_args])
            return f"launched: {safe_app_path}"
    except SecurityViolation as e:
        return f"[security] {e}"
    except Exception:
        pass

    try:
        resolved = shutil.which(app_target)
        if resolved:
            safe_resolved = _safe_path(resolved, mode="execute")
            subprocess.Popen([safe_resolved, *app_args])
            return f"launched: {safe_resolved}"
    except SecurityViolation as e:
        return f"[security] {e}"
    except Exception:
        pass

    try:
        if _IS_WINDOWS:
            cmd = f"Start-Process {_ps_single_quote(app_target)}"
            if app_args:
                cmd += " -ArgumentList " + _ps_single_quote(" ".join(app_args))
            result = await _sandbox.execute_shell_command(cmd=cmd, timeout=20)
            if result.get("ok", False):
                return f"launched: {app_target}"
            return _format_result(result)

        if platform.system() == "Darwin":
            cmd = ["open", app_target, *app_args]
        else:
            safe_app_target = _safe_path(app_target, mode="execute")
            cmd = [safe_app_target, *app_args]
        # Use blocking Popen in a thread to avoid asyncio subprocess transport
        try:
            popen = await asyncio.to_thread(subprocess.Popen, cmd)
            pid = getattr(popen, 'pid', None)
            return f"launched: {app_target} (pid={pid})"
        except SecurityViolation as e:
            return f"[security] {e}"
        except Exception as e:
            return f"[error] launch failed: {e}"
    except SecurityViolation as e:
        return f"[security] {e}"
    except Exception as e:
        return f"[error] launch failed: {e}"


async def run_script(code: str, lang: str = "python", cwd: str = "", timeout: int = 60) -> str:
    safe_cwd = _safe_path(cwd, mode="execute") if cwd.strip() else None
    return await _sandbox.execute_script(code=code, lang=lang, cwd=safe_cwd, timeout=timeout)


def list_directory(path: str = ".") -> str:
    try:
        safe_path = _safe_path(path, mode="read")
    except SecurityViolation as e:
        return f"[security] {e}"

    p = Path(safe_path)
    if not p.exists() or not p.is_dir():
        return f"[error] not a directory: {safe_path}"

    items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    if not items:
        return "(empty directory)"

    lines = []
    for item in items[:300]:
        suffix = "/" if item.is_dir() else ""
        lines.append(f"{item.name}{suffix}")
    return "\n".join(lines)


# MCP 兼容别名

def list_dir(path: str = ".", depth: int = 2) -> str:
    _ = depth
    return list_directory(path=path)


def read_text_file(path: str, max_chars: int = 8000) -> str:
    try:
        safe_path = _safe_path(path, mode="read")
    except SecurityViolation as e:
        return f"[security] {e}"

    p = Path(safe_path)
    if not p.exists() or not p.is_file():
        return f"[error] file not found: {safe_path}"

    try:
        limited = read_text_limited(
            p,
            encoding="utf-8",
            errors="replace",
            max_bytes=min(MAX_TEXT_READ_BYTES, max(max_chars * 4, 4096)),
        )
        text = limited.text
    except Exception as e:
        return f"[error] read failed: {e}"

    if len(text) > max_chars:
        return text[:max_chars] + "\n...\n[truncated]"
    if limited.truncated:
        return text + "\n...\n[truncated by byte limit]"
    return text


# MCP 兼容别名

def read_file(path: str, encoding: str = "utf-8", max_chars: int = 20000) -> str:
    try:
        safe_path = _safe_path(path, mode="read")
        limited = read_text_limited(
            safe_path,
            encoding=encoding,
            errors="replace",
            max_bytes=min(MAX_TEXT_READ_BYTES, max(max_chars * 4, 4096)),
        )
        text = limited.text
        if len(text) > max_chars:
            return text[:max_chars] + "\n...\n[truncated]"
        if limited.truncated:
            return text + "\n...\n[truncated by byte limit]"
        return text
    except Exception as e:
        return f"[error] {e}"


from pawmate.tools.text.encoding import inspect_encoding, atomic_write_text


def write_file(path: str, content: str, encoding: str = "utf-8") -> dict:
    """原子写入文本文件，带编码预检和 postcondition 验证。

    返回 dict（调用方序列化为 JSON 给 LLM）。
    """
    # 路径安全校验
    try:
        safe_path = _safe_path(path, mode="write")
    except SecurityViolation as e:
        return {
            "ok": False,
            "error_type": "security",
            "operation": "write_file",
            "path": path,
            "message": str(e),
        }

    target = Path(safe_path)
    already_existed = target.exists()
    old_content = None

    # 读取旧内容以在后验失败时恢复
    if already_existed:
        try:
            old_content = target.read_bytes()
        except Exception:
            old_content = None

    # ── Step 1: 编码预检 ──
    encoding_issue = inspect_encoding(content, encoding=encoding, field="content")
    if encoding_issue is not None:
        return {
            "ok": False,
            "error_type": "encoding_error",
            "operation": "write_file",
            "stage": "encode_before_write",
            "field": encoding_issue.field,
            "encoding": encoding_issue.encoding,
            "offending_text": encoding_issue.offending_text,
            "codepoints": encoding_issue.codepoints,
            "position": {"start": encoding_issue.start, "end": encoding_issue.end},
            "message": encoding_issue.message,
            "path": str(target),
            "recoverable": True,
            "suggested_action": "Remove the unsupported character or retry with UTF-8.",
            "postcondition": {
                "target_created": target.exists(),
                "write_performed": False,
            },
        }

    # ── Step 2: 原子写入 ──
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, content, encoding=encoding)
    except Exception as exc:
        # 写入失败，已存在的旧文件保留原样（tmp 文件已清理）
        return {
            "ok": False,
            "error_type": "write_error",
            "operation": "write_file",
            "stage": "write",
            "path": str(target),
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "recoverable": False,
            "postcondition": {"target_created": target.exists()},
        }

    # ── Step 3: Postcondition 验证 ──
    if not target.exists():
        # 已存在文件的旧内容仍然存在（因为原子写入失败不碰原文件）
        return {
            "ok": False,
            "error_type": "postcondition_failed",
            "operation": "write_file",
            "stage": "verify_after_write",
            "path": str(target),
            "recoverable": False,
            "postcondition": {"target_created": False},
        }

    size_bytes = target.stat().st_size
    return {
        "ok": True,
        "operation": "write_file",
        "path": str(target),
        "encoding": encoding,
        "size_bytes": size_bytes,
        "postcondition": {
            "target_created": True,
        },
    }


def search_file(pattern: str, root: str = ".", file_glob: str = "*.py") -> str:
    try:
        safe_root = _safe_path(root, mode="read")
    except SecurityViolation as e:
        return f"[security] {e}"

    matches: List[str] = []
    for fp in Path(safe_root).rglob(file_glob):
        try:
            assert_text_file_readable(fp)
            limited = read_text_limited(fp, errors="replace", max_bytes=512 * 1024)
            for i, line in enumerate(limited.text.splitlines(), 1):
                if pattern in line:
                    matches.append(f"{fp}:{i}: {line.strip()}")
        except (FileBoundaryError, UnicodeDecodeError):
            continue
        except Exception:
            continue

    if not matches:
        return "no matches"
    return "\n".join(matches[:200])


async def run_tests(path: str = ".", filter_: str = "", timeout: int = 120) -> str:
    try:
        safe_path = _safe_path(path, mode="read")
    except SecurityViolation as e:
        return f"[security] {e}"
    quoted_path = _ps_single_quote(safe_path) if _IS_WINDOWS else shlex.quote(safe_path)
    cmd = f"pytest {quoted_path} -x -q --tb=short"
    if filter_:
        safe_filter = str(filter_).replace("'", " ").replace('"', " ").strip()
        quoted_filter = _ps_single_quote(safe_filter) if _IS_WINDOWS else shlex.quote(safe_filter)
        cmd += f" -k {quoted_filter}"
    result = await _sandbox.execute_shell_command(cmd=cmd, timeout=min(timeout, 120))
    return _format_result(result)


def read_symbol(path: str, symbol: str) -> str:
    try:
        safe_path = _safe_path(path, mode="read")
        limited = read_text_limited(safe_path, encoding="utf-8", errors="replace")
        if limited.truncated:
            return f"[error] file too large for symbol read: {safe_path} ({limited.size_bytes} bytes)"
        src = limited.text
        tree = ast.parse(src)
    except Exception as e:
        return f"[error] parse failed: {e}"

    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
            start = node.lineno - 1
            end = node.end_lineno
            return "\n".join(lines[start:end])
    return f"[error] symbol not found: {symbol}"


# ════════════════════════════════════════════════════════════
# Known Folder 工具
# ════════════════════════════════════════════════════════════


def get_known_folder(name: str) -> str:
    """
    获取系统已知文件夹的绝对路径。

    Supported names:
      - desktop: Windows 桌面（含 OneDrive 重定向）
      - downloads: 下载目录
      - documents: 文档目录
      - home: 用户家目录

    Windows 平台优先使用 Windows Known Folder API。
    """
    name = name.strip().lower()

    if _IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            # KNOWNFOLDERID 常量
            FOLDERID = {
                "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
                "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
                "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
            }

            clsid = FOLDERID.get(name)
            if clsid:
                # SHGetKnownFolderPath
                SHGetKnownFolderPath = ctypes.windll.shell32.SHGetKnownFolderPath
                SHGetKnownFolderPath.restype = ctypes.c_long
                SHGetKnownFolderPath.argtypes = [
                    ctypes.c_char_p,  # KNOWNFOLDERID
                    ctypes.c_uint32,  # flags (0 = default)
                    wintypes.HANDLE,  # token (NULL)
                    ctypes.POINTER(ctypes.c_wchar_p),  # path out
                ]

                p_path = ctypes.c_wchar_p()
                guid = clsid.encode("utf-8")
                hr = SHGetKnownFolderPath(guid, 0, None, ctypes.byref(p_path))
                if hr == 0 and p_path.value:
                    return p_path.value

                # SHGetKnownFolderPath 失败时回退
        except Exception:
            pass

    fallbacks = {
        "desktop": str(Path.home() / "Desktop"),
        "downloads": str(Path.home() / "Downloads"),
        "documents": str(Path.home() / "Documents"),
        "home": str(Path.home()),
    }

    if name in fallbacks:
        return fallbacks[name]

    return f"[error] unknown folder: {name}"


def list_desktop() -> str:
    """列出 Windows 桌面文件列表。不用传路径。"""
    desktop = get_known_folder("desktop")
    if desktop.startswith("[error]"):
        return desktop
    return list_directory(path=desktop)


def create_directory(path: str, exist_ok: bool = True) -> dict:
    """
    创建目录（含父目录）。需要用户授权。
    返回 dict（序列化为 JSON）。
    """
    try:
        safe = _safe_path(path, mode="write")
    except SecurityViolation as e:
        return {"ok": False, "error_type": "security", "operation": "create_directory", "message": str(e)}

    try:
        p = Path(safe)
        p.mkdir(parents=True, exist_ok=exist_ok)
        exists = p.exists() and p.is_dir()
        return {
            "ok": exists,
            "operation": "create_directory",
            "path": str(p.resolve()),
            "postcondition": {
                "directory_exists": exists,
                "is_directory": True,
            },
        }
    except Exception as e:
        return {
            "ok": False,
            "error_type": "create_directory_error",
            "operation": "create_directory",
            "exception_type": type(e).__name__,
            "message": str(e),
        }


def move_path(source: str, destination: str, overwrite: bool = False) -> dict:
    """
    移动文件或目录。
    需要用户授权。默认不覆盖已有文件。
    返回 dict（序列化为 JSON）。
    """
    try:
        safe_src = _safe_path(source, mode="move")
        safe_dst = _safe_path(destination, mode="move")
    except SecurityViolation as e:
        return {"ok": False, "error_type": "security", "operation": "move_path", "message": str(e)}

    src = Path(safe_src)
    dst = Path(safe_dst)

    if not src.exists():
        return {"ok": False, "error_type": "move_error", "operation": "move_path", "message": f"source not found: {safe_src}"}

    if dst.exists() and not overwrite:
        return {"ok": False, "error_type": "move_error", "operation": "move_path", "message": f"destination exists and overwrite=False: {safe_dst}"}

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        src_gone = not Path(safe_src).exists()
        dst_exists = Path(safe_dst).exists()
        return {
            "ok": src_gone and dst_exists,
            "operation": "move_path",
            "source": str(src),
            "destination": str(dst),
            "postcondition": {"source_gone": src_gone, "destination_exists": dst_exists},
        }
    except Exception as e:
        return {"ok": False, "error_type": "move_error", "operation": "move_path", "exception_type": type(e).__name__, "message": str(e)}


def move_paths(sources: List[str], destination_dir: str, overwrite: bool = False) -> dict:
    """
    批量移动文件/目录到目标目录。
    每个 source 单独校验。
    整个批量操作只弹一次授权确认。
    返回 dict（序列化为 JSON）。
    """
    if not sources:
        return {"ok": False, "error_type": "move_error", "operation": "move_paths", "message": "sources list empty"}

    try:
        safe_dst_dir = _safe_path(destination_dir, mode="move")
    except SecurityViolation as e:
        return {"ok": False, "error_type": "security", "operation": "move_paths", "message": str(e)}

    dst_root = Path(safe_dst_dir)
    items: list[dict] = []
    moved_count = 0
    skipped_count = 0
    failed_count = 0

    for src_path in sources:
        try:
            safe_src = _safe_path(src_path, mode="move")
        except SecurityViolation as e:
            items.append({"source": src_path, "status": "failed", "reason": f"security: {e}"})
            failed_count += 1
            continue

        src = Path(safe_src)
        if not src.exists():
            items.append({"source": safe_src, "status": "skipped", "reason": "source not found"})
            skipped_count += 1
            continue

        dst = dst_root / src.name
        if dst.exists() and not overwrite:
            items.append({"source": safe_src, "destination": str(dst), "status": "skipped", "reason": "destination exists"})
            skipped_count += 1
            continue

        try:
            dst_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            if not Path(safe_src).exists() and Path(str(dst)).exists():
                items.append({"source": str(src), "destination": str(dst), "status": "moved"})
                moved_count += 1
            else:
                items.append({"source": str(src), "status": "failed", "reason": "postcondition failed"})
                failed_count += 1
        except Exception as e:
            items.append({"source": safe_src, "status": "failed", "reason": str(e)})
            failed_count += 1

    return {
        "ok": failed_count == 0,
        "operation": "move_paths",
        "destination_dir": safe_dst_dir,
        "summary": {"moved": moved_count, "skipped": skipped_count, "failed": failed_count},
        "items": items,
    }


def register_builtin_tools(
    registry: ToolRegistry,
    security_service: SecurityService | None = None,
) -> None:
    # Hosted/provider-native search is separate from browser automation.
    register_native_web_search_tools(registry)

    # Register only the four intent-level browser facade tools for LLM use.
    register_browser_tools(registry)
    if os.getenv("PAWMATE_ENABLE_LEGACY_CHATGPT_BRIDGE", "").strip().lower() in {"1", "true", "yes", "on"}:
        from pawmate.tools.chatgpt.bridge import register_chatgpt_bridge_tools

        register_chatgpt_bridge_tools(registry)

    # 启动时诊断浏览器依赖（非阻塞）
    log_browser_diagnostics()

    # 注册下载工具
    register_download_tools(registry, security_service=security_service)

    # 注册其他内置工具
    defs = [
        ToolDef(
            name="run_powershell_query",
            description=(
                "Silently run a read-only PowerShell query for local Windows inspection "
                "(startup items, desktop files/folders, processes, services, system info, registry reads). "
                "Use this before saying the computer cannot be inspected. "
                "For changes, writes, launches, installs, or destructive operations use run_shell_command."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20},
                },
                "required": ["command"],
            },
            handler=run_powershell_query,
            approval=APPROVAL_AUTO,
            category=ToolCategory.SHELL,
            risk=RiskLevel.MEDIUM,
            side_effect=SideEffectLevel.READ_ONLY,
            tags=["powershell", "read_only", "operation_log"],
            timeout=65,
        ),
        ToolDef(
            name="run_shell_command",
            description="Run a shell command with cwd and timeout; requires confirmation.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20},
                },
                "required": ["command"],
            },
            handler=run_shell_command,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.SHELL,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.PRIVILEGED,
            timeout=65,
        ),
        ToolDef(
            name="run_command",
            description="Alias of run_shell_command; requires confirmation.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20},
                },
                "required": ["command"],
            },
            handler=run_command,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.SHELL,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.PRIVILEGED,
            timeout=65,
            model_visible=False,
        ),
        ToolDef(
            name="open_url",
            description="Open URL in the system default browser; prefer browser_* for automation.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                },
                "required": ["url"],
            },
            handler=open_url,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.NETWORK,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.EXTERNAL_WRITE,
        ),
        ToolDef(
            name="launch_desktop_app",
            description="Launch a local app or file with optional args; requires confirmation.",
            input_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "args": {"type": "string", "default": ""},
                },
                "required": ["target"],
            },
            handler=launch_desktop_app,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.SYSTEM,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.PRIVILEGED,
        ),
        ToolDef(
            name="run_script",
            description="Run python/powershell/bash/node snippet with timeout; requires confirmation.",
            input_schema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "lang": {"type": "string", "default": "python"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 120, "default": 60},
                },
                "required": ["code"],
            },
            handler=run_script,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.SHELL,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.PRIVILEGED,
            timeout=125,
        ),
        ToolDef(
            name="list_directory",
            description="List files/folders in a directory; use list_desktop or get_known_folder for user folders.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
            handler=list_directory,
            approval=APPROVAL_NOTIFY,
            category=ToolCategory.FILE,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
        ),
        ToolDef(
            name="list_dir",
            description="Alias of list_directory with depth; use list_desktop for desktop contents.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "depth": {"type": "integer", "default": 2},
                },
                "required": [],
            },
            handler=list_dir,
            approval=APPROVAL_NOTIFY,
            category=ToolCategory.FILE,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
            model_visible=False,
        ),
        ToolDef(
            name="read_text_file",
            description="Read text file content with max_chars truncation; requires confirmation.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 200, "maximum": 40000, "default": 8000},
                },
                "required": ["path"],
            },
            handler=read_text_file,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.FILE,
            risk=RiskLevel.MEDIUM,
            side_effect=SideEffectLevel.READ_ONLY,
        ),
        ToolDef(
            name="read_file",
            description="Read a file using encoding; requires confirmation.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "encoding": {"type": "string", "default": "utf-8"},
                    "max_chars": {"type": "integer", "minimum": 200, "maximum": 40000, "default": 20000},
                },
                "required": ["path"],
            },
            handler=read_file,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.FILE,
            risk=RiskLevel.MEDIUM,
            side_effect=SideEffectLevel.READ_ONLY,
            model_visible=False,
        ),
        ToolDef(
            name="write_file",
            description="Write text to a file atomically; returns [ok] on success and requires confirmation.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "encoding": {"type": "string", "default": "utf-8"},
                },
                "required": ["path", "content"],
            },
            handler=write_file,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.FILE,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.LOCAL_WRITE,
        ),
        ToolDef(
            name="search_file",
            description="Search text pattern recursively under root with file_glob.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "root": {"type": "string", "default": "."},
                    "file_glob": {"type": "string", "default": "*.py"},
                },
                "required": ["pattern"],
            },
            handler=search_file,
            approval=APPROVAL_NOTIFY,
            category=ToolCategory.FILE,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
        ),
        ToolDef(
            name="run_tests",
            description="Run pytest for path/filter and return summarized output.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "filter_": {"type": "string", "default": ""},
                    "timeout": {"type": "integer", "minimum": 10, "maximum": 120, "default": 120},
                },
                "required": [],
            },
            handler=run_tests,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.SHELL,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.PRIVILEGED,
            timeout=125,
        ),
        ToolDef(
            name="read_symbol",
            description="Read a Python function/class source by symbol name.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "symbol": {"type": "string"},
                },
                "required": ["path", "symbol"],
            },
            handler=read_symbol,
            approval=APPROVAL_NOTIFY,
            category=ToolCategory.FILE,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
        ),
        ToolDef(
            name="capture_screenshot",
            description="Capture desktop screenshot to path; use browser_screenshot for web pages.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "screenshot.png"},
                },
                "required": ["path"],
            },
            handler=capture_screenshot,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.UI,
            risk=RiskLevel.MEDIUM,
            side_effect=SideEffectLevel.LOCAL_WRITE,
        ),
        ToolDef(
            name="ocr_image",
            description="Analyze image with vision model; request JSON coordinates for target tasks.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "lang": {"type": "string", "default": "eng"},
                    "prompt": {"type": "string", "default": ""},
                },
                "required": ["path"],
            },
            handler=ocr_image,
            approval=APPROVAL_NOTIFY,
            category=ToolCategory.FILE,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
        ),
        ToolDef(
            name="vision_analyze",
            description="Answer questions about an image; return bbox/point JSON for UI targets.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "prompt": {"type": "string", "default": "请描述这张图片并提取可见文字。"},
                    "lang": {"type": "string", "default": "eng"},
                },
                "required": ["path"],
            },
            handler=vision_analyze,
            approval=APPROVAL_NOTIFY,
            category=ToolCategory.FILE,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
        ),
        # ── Known Folder 工具 ────────────────────────────
        ToolDef(
            name="get_known_folder",
            description="Get absolute path for desktop, downloads, documents, or home.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": ["desktop", "downloads", "documents", "home"],
                    },
                },
                "required": ["name"],
            },
            handler=get_known_folder,
            approval=APPROVAL_NOTIFY,
            category=ToolCategory.FILE,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
        ),
        ToolDef(
            name="list_desktop",
            description="List files/folders on the real Windows desktop path.",
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=list_desktop,
            approval=APPROVAL_NOTIFY,
            category=ToolCategory.FILE,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
        ),
        # ── 文件操作工具（需要授权） ─────────────────────
        ToolDef(
            name="create_directory",
            description="Create a directory and parents if needed; requires confirmation.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to create"},
                    "exist_ok": {"type": "boolean", "default": True},
                },
                "required": ["path"],
            },
            handler=create_directory,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.FILE,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.LOCAL_WRITE,
        ),
        ToolDef(
            name="move_path",
            description="Move one file/directory; no overwrite by default; requires confirmation.",
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Source path"},
                    "destination": {"type": "string", "description": "Destination path"},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["source", "destination"],
            },
            handler=move_path,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.FILE,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.LOCAL_WRITE,
        ),
        ToolDef(
            name="move_paths",
            description="Move multiple files/directories into target directory; requires confirmation.",
            input_schema={
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of source paths to move",
                    },
                    "destination_dir": {"type": "string", "description": "Target directory"},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["sources", "destination_dir"],
            },
            handler=move_paths,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.FILE,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.LOCAL_WRITE,
        ),
    ]

    for tool_def in defs:
        registry.register(tool_def)

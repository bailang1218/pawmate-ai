"""
PawMate MCP Server — 统一的工具能力集合

用 FastMCP 写，单文件，engine 通过 subprocess stdio 调用。
提供五组工具：shell / fs / code / system / memory
使用安全沙箱限制危险操作

pip install mcp fastmcp
"""
from __future__ import annotations
import asyncio
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import shlex
import webbrowser
from pathlib import Path
from typing import Any, Optional

# 作为脚本运行时，确保能导入到 pawmate 包。
if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from mcp.server.fastmcp import FastMCP
from pawmate.core.resource_limits import MAX_TEXT_READ_BYTES, read_text_limited
from pawmate.tools.text_encoding import atomic_write_text, inspect_encoding

# 导入安全沙箱 - 支持两种导入模式（相对导入和绝对导入）
# 安全性：Sandbox 导入失败时必须 fail closed，禁止退化为无保护执行。
try:
    from .sandbox import Sandbox, SecurityViolation
except (ImportError, ValueError):
    # 作为 MCP 服务器脚本运行时，相对导入会失败
    # 尝试绝对导入
    try:
        from pawmate.tools.sandbox import Sandbox, SecurityViolation
    except Exception as exc:
        raise RuntimeError(
            f"Sandbox import failed; MCP server refuses to start: {exc}"
        ) from exc

mcp = FastMCP("pawmate")

IS_WIN = platform.system() == "Windows"
# 创建全局沙箱实例
sandbox = Sandbox(max_runtime=30, max_output_size=1024*1024)  # 30秒，1MB输出限制


def _safe_mcp_path(path: str) -> str:
    """在 MCP 工具中统一使用路径校验，避免绕过宿主安全策略。"""
    try:
        return CommandValidator.sanitize_path(path)
    except SecurityViolation as e:
        raise SecurityViolation(f"MCP 路径违规: {e}") from e

# ════════════════════════════════════════════════════════
#  platform_shim — 所有工具通过这里执行命令
# ════════════════════════════════════════════════════════

async def _exec(cmd: str, cwd: str | None = None, timeout: int = 30) -> dict:
    """统一命令执行，Win 用 PowerShell，Linux 用 bash，通过安全沙箱"""
    try:
        result = await sandbox.execute_shell_command(cmd, cwd, timeout)
        return result
    except SecurityViolation as e:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": f"安全违规: {str(e)}"}
    except Exception as e:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": str(e)}


def _strip_wrapping_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
        return v[1:-1]
    return v


def _ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _normalize_command_for_platform(command: str) -> str:
    cmd = command.strip()
    if not cmd:
        return command

    if IS_WIN:
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


def _split_app_args(args: str) -> list[str]:
    if not args.strip():
        return []
    try:
        return shlex.split(args, posix=not IS_WIN)
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


def _maybe_launch_vscode(command: str) -> Optional[str]:
    cmd = command.strip()
    if not cmd:
        return None

    try:
        parts = shlex.split(cmd, posix=not IS_WIN)
    except Exception:
        return None

    if not parts or parts[0].lower() != "code":
        return None

    launcher = _resolve_vscode_launcher()
    args = " ".join(parts[1:])
    return f"{launcher} {args}".strip() if args else launcher


# ════════════════════════════════════════════════════════
#  shell 组
# ════════════════════════════════════════════════════════

@mcp.tool()
async def run_command(command: str, cwd: str = "", timeout: int = 30) -> str:
    """
    执行 shell 命令。
    Win 用 PowerShell，Linux 用 bash，自动适配。
    打开链接建议优先使用 open_url。
    返回 stdout + stderr 合并文本。
    """
    vscode_command = _maybe_launch_vscode(command)
    normalized_command = vscode_command or _normalize_command_for_platform(command)
    result = await _exec(normalized_command, cwd=cwd or None, timeout=timeout)
    lines = []
    if result["stdout"]:
        lines.append(result["stdout"])
    if result["stderr"]:
        lines.append(f"[stderr] {result['stderr']}")
    if not lines:
        lines.append("(无输出)")
    if not result["ok"]:
        lines.append(f"[退出码 {result['rc']}]")
    return "\n".join(lines)


@mcp.tool()
async def open_url(url: str) -> str:
    """在系统默认浏览器打开 URL。"""
    target = _normalize_url(url)
    if not target:
        return "[错误] URL 不能为空"

    try:
        opened = await asyncio.to_thread(webbrowser.open, target, 2, True)
        if opened:
            return f"已打开: {target}"
    except Exception:
        # 回退到 shell 命令执行
        pass

    if IS_WIN:
        cmd = _normalize_command_for_platform(f"open {target}")
    elif platform.system() == "Darwin":
        cmd = f'open "{target}"'
    else:
        cmd = f'xdg-open "{target}"'

    result = await _exec(cmd, timeout=20)
    if result["ok"]:
        return f"已打开: {target}"

    lines = []
    if result["stderr"]:
        lines.append(f"[stderr] {result['stderr']}")
    lines.append(f"[退出码 {result.get('rc', -1)}]")
    return "\n".join(lines)


@mcp.tool()
async def launch_desktop_app(target: str, args: str = "") -> str:
    """启动本地桌面应用或本地文件。桌面应用请优先用这个，而不是 open_url。"""
    app_target = (target or "").strip()
    if not app_target:
        return "[错误] target 不能为空"

    app_args = _split_app_args(args)

    try:
        app_path = Path(app_target).expanduser()
        if app_path.exists():
            if IS_WIN:
                os.startfile(str(app_path))
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(app_path), *app_args])
            else:
                subprocess.Popen([str(app_path), *app_args])
            return f"已启动: {app_path}"
    except Exception:
        pass

    try:
        resolved = shutil.which(app_target)
        if resolved:
            subprocess.Popen([resolved, *app_args])
            return f"已启动: {resolved}"
    except Exception:
        pass

    try:
        if IS_WIN:
            cmd = f"Start-Process {_ps_single_quote(app_target)}"
            if app_args:
                cmd += " -ArgumentList " + _ps_single_quote(" ".join(app_args))
            result = await _exec(cmd, timeout=20)
            if result["ok"]:
                return f"已启动: {app_target}"
            lines = []
            if result["stderr"]:
                lines.append(f"[stderr] {result['stderr']}")
            lines.append(f"[退出码 {result.get('rc', -1)}]")
            return "\n".join(lines)

        if platform.system() == "Darwin":
            cmd = ["open", app_target, *app_args]
        else:
            cmd = [app_target, *app_args]
        try:
            popen = await asyncio.to_thread(subprocess.Popen, cmd)
            pid = getattr(popen, 'pid', None)
            return f"已启动: {app_target} (pid={pid})"
        except Exception as e:
            return f"[错误] 启动失败: {e}"
    except Exception as e:
        return f"[错误] 启动失败: {e}"


@mcp.tool()
async def run_script(
    code: str,
    lang: str = "python",
    cwd: str = "",
    timeout: int = 60,
) -> str:
    """
    执行代码片段。
    lang: python | powershell | bash | node
    通过安全沙箱执行，限制资源使用。
    """
    try:
        result = await sandbox.execute_script(code, lang, cwd or None, timeout)
        return result
    except SecurityViolation as e:
        return f"[安全违规] {str(e)}"
    except Exception as e:
        return f"[错误] 脚本执行失败: {str(e)}"


@mcp.tool()
async def kill_process(pid: int) -> str:
    """强制终止指定 PID 的进程"""
    if IS_WIN:
        r = await _exec(f"Stop-Process -Id {pid} -Force")
    else:
        r = await _exec(f"kill -9 {pid}")
    return "已终止" if r["ok"] else f"失败: {r['stderr']}"


# ════════════════════════════════════════════════════════
#  fs 组
# ════════════════════════════════════════════════════════

@mcp.tool()
def read_file(path: str, encoding: str = "utf-8") -> str:
    """读取文件全部内容"""
    try:
        safe = _safe_mcp_path(path)
        limited = read_text_limited(safe, encoding=encoding, errors="replace")
        if limited.truncated:
            return (
                limited.text
                + f"\n\n[truncated: file is {limited.size_bytes} bytes; "
                + f"showing first {MAX_TEXT_READ_BYTES} bytes]"
            )
        return limited.text
    except SecurityViolation as e:
        return f"[安全违规] {e}"
    except FileNotFoundError:
        return f"[错误] 文件不存在: {path}"
    except Exception as e:
        return f"[错误] {e}"


@mcp.tool()
def write_file(path: str, content: str, encoding: str = "utf-8") -> str:
    """覆盖写入文件（自动创建父目录），编码预检 + 原子写入。"""
    try:
        safe = _safe_mcp_path(path)
    except SecurityViolation as e:
        return f"[security] {e}"
    target = Path(safe)

    # Step 1: 编码预检
    issue = inspect_encoding(content, encoding=encoding, field="content")
    if issue is not None:
        cp = " ".join(f"U+{ord(ch):04X}" for ch in issue.offending_text)
        return (
            f"[write_encoding_error] encoding={encoding}; "
            f"offending={issue.offending_text!r}; codepoints=[{cp}]; "
            f"field={issue.field}; path={safe}"
        )

    # Step 2: 原子写入
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, content, encoding=encoding)
    except Exception as exc:
        return f"[write_error] {type(exc).__name__}: {exc}"

    # Step 3: Postcondition
    if not target.exists():
        return f"[write_error] target missing after write: {safe}"
    size = target.stat().st_size
    return f"[ok] written: {safe} ({len(content)} chars, {size} bytes, encoding={encoding})"


@mcp.tool()
def patch_file(path: str, old: str, new: str, encoding: str = "utf-8") -> str:
    """
    在文件中精确替换字符串（只替换第一次出现）。
    用于代码修改，比整体覆写安全。
    """
    try:
        safe = _safe_mcp_path(path)
        p = Path(safe)
        limited = read_text_limited(p, encoding=encoding, errors="replace")
        if limited.truncated:
            return (
                f"[error] file too large for patch_file: {limited.size_bytes} bytes "
                f"> {MAX_TEXT_READ_BYTES} bytes"
            )
        text = limited.text
        if old not in text:
            return f"[错误] 目标字符串未找到，未做修改"
        p.write_text(text.replace(old, new, 1), encoding=encoding)
        return "替换成功"
    except SecurityViolation as e:
        return f"[安全违规] {e}"
    except Exception as e:
        return f"[错误] {e}"


@mcp.tool()
def list_dir(path: str = ".", depth: int = 2) -> str:
    """
    列出目录结构（树形）。
    depth: 最大递归深度，默认 2，避免爆炸性输出。
    """
    def _tree(p: Path, d: int, prefix: str = "") -> list[str]:
        if d < 0:
            return []
        lines = []
        try:
            items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        except PermissionError:
            return [prefix + "[权限不足]"]
        for i, item in enumerate(items):
            connector = "└── " if i == len(items) - 1 else "├── "
            lines.append(prefix + connector + item.name + ("/" if item.is_dir() else ""))
            if item.is_dir() and d > 0:
                ext = "    " if i == len(items) - 1 else "│   "
                lines.extend(_tree(item, d - 1, prefix + ext))
        return lines

    try:
        safe = _safe_mcp_path(path)
    except SecurityViolation as e:
        return f"[安全违规] {e}"
    p = Path(safe)
    if not p.exists():
        return f"[错误] 路径不存在: {safe}"
    result = [str(p.resolve())]
    result.extend(_tree(p, depth))
    return "\n".join(result)


@mcp.tool()
async def search_file(
    pattern: str,
    root: str = ".",
    file_glob: str = "*.py",
) -> str:
    """
    在目录下递归搜索文件内容（类 grep）。
    pattern: 搜索字符串（非正则）。
    返回 文件:行号: 内容 格式。
    """
    try:
        safe_root = _safe_mcp_path(root)
    except SecurityViolation as e:
        return f"[安全违规] {e}"
    results = []
    for fp in Path(safe_root).rglob(file_glob):
        try:
            limited = read_text_limited(fp, errors="replace", max_bytes=512 * 1024)
            for i, line in enumerate(limited.text.splitlines(), 1):
                if pattern in line:
                    results.append(f"{fp}:{i}: {line.strip()}")
        except Exception:
            pass
    if not results:
        return "未找到匹配"
    return "\n".join(results[:200])  # 最多返回 200 行


@mcp.tool()
def delete_path(path: str) -> str:
    """删除文件或目录（目录递归删除）"""
    try:
        safe = _safe_mcp_path(path)
    except SecurityViolation as e:
        return f"[安全违规] {e}"
    p = Path(safe)
    if not p.exists():
        return "路径不存在，无需删除"
    try:
        if p.is_file():
            p.unlink()
        else:
            shutil.rmtree(p)
        return f"已删除 {safe}"
    except Exception as e:
        return f"[错误] {e}"


# ════════════════════════════════════════════════════════
#  code 组（AST 级别操作）
# ════════════════════════════════════════════════════════

@mcp.tool()
def read_symbol(path: str, symbol: str) -> str:
    """
    从 Python 文件中提取指定函数或类的完整源码。
    symbol: 函数名或类名。
    用 ast 解析，比正则可靠。
    """
    import ast
    try:
        safe = _safe_mcp_path(path)
        limited = read_text_limited(safe, encoding="utf-8", errors="replace")
        if limited.truncated:
            return (
                f"[error] file too large for read_symbol: {limited.size_bytes} bytes "
                f"> {MAX_TEXT_READ_BYTES} bytes"
            )
        src = limited.text
        tree = ast.parse(src)
    except Exception as e:
        return f"[错误] 解析失败: {e}"

    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                start = node.lineno - 1
                end   = node.end_lineno
                return "\n".join(lines[start:end])
    return f"[错误] 符号 '{symbol}' 未找到"


@mcp.tool()
def replace_symbol(path: str, symbol: str, new_code: str) -> str:
    """
    替换 Python 文件中指定函数或类的完整定义。
    用 ast 定位行号，精确替换，不影响其他代码。
    """
    import ast
    try:
        safe = _safe_mcp_path(path)
        limited = read_text_limited(safe, encoding="utf-8", errors="replace")
        if limited.truncated:
            return (
                f"[error] file too large for replace_symbol: {limited.size_bytes} bytes "
                f"> {MAX_TEXT_READ_BYTES} bytes"
            )
        src = limited.text
        tree = ast.parse(src)
    except Exception as e:
        return f"[错误] 解析失败: {e}"

    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                start = node.lineno - 1
                end   = node.end_lineno
                new_lines = lines[:start] + new_code.splitlines() + lines[end:]
                Path(safe).write_text("\n".join(new_lines), encoding="utf-8")
                return f"已替换 {symbol}（原 {end-start} 行 → 新 {len(new_code.splitlines())} 行）"
    return f"[错误] 符号 '{symbol}' 未找到"


@mcp.tool()
async def run_tests(
    path: str = ".",
    filter_: str = "",
    timeout: int = 120,
) -> str:
    """
    运行 pytest，返回结果摘要。
    filter_: pytest -k 表达式，留空跑全部。
    """
    cmd = f"pytest {path} -x -q --tb=short"
    if filter_:
        cmd += f' -k "{filter_}"'
    r = await _exec(cmd, timeout=timeout)
    return r["stdout"] + ("\n" + r["stderr"] if r["stderr"] else "")


# ════════════════════════════════════════════════════════
#  memory 组（简单 KV，存在本地 JSON）
# ════════════════════════════════════════════════════════

_MEMORY_FILE = Path.home() / ".pawmate_mcp_memory.json"

def _load_mem() -> dict:
    try:
        return json.loads(_MEMORY_FILE.read_text()) if _MEMORY_FILE.exists() else {}
    except Exception:
        return {}

def _save_mem(data: dict):
    _MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


@mcp.tool()
def remember(key: str, value: str) -> str:
    """保存一条记忆（key-value）"""
    mem = _load_mem()
    mem[key] = value
    _save_mem(mem)
    return f"已记住 [{key}]"


@mcp.tool()
def recall(key: str) -> str:
    """读取一条记忆"""
    mem = _load_mem()
    return mem.get(key, f"[未找到] key={key}")


@mcp.tool()
def list_memories() -> str:
    """列出所有记忆的 key"""
    keys = list(_load_mem().keys())
    return "\n".join(keys) if keys else "（空）"


@mcp.tool()
def forget(key: str) -> str:
    """删除一条记忆"""
    mem = _load_mem()
    if key in mem:
        del mem[key]
        _save_mem(mem)
        return f"已删除 [{key}]"
    return f"[未找到] key={key}"


# ════════════════════════════════════════════════════════
#  system 组
# ════════════════════════════════════════════════════════

@mcp.tool()
def get_env(name: str = "") -> str:
    """读取环境变量。name 为空时返回全部（过滤敏感词）"""
    SENSITIVE = {"KEY", "TOKEN", "SECRET", "PASS", "PWD", "CREDENTIAL"}
    if name:
        return os.environ.get(name, f"[未设置] {name}")
    safe = {
        k: v for k, v in os.environ.items()
        if not any(s in k.upper() for s in SENSITIVE)
    }
    return "\n".join(f"{k}={v}" for k, v in sorted(safe.items()))


@mcp.tool()
async def list_processes(filter_name: str = "") -> str:
    """列出当前进程（可按名称过滤）"""
    if IS_WIN:
        cmd = 'Get-Process | Select-Object Id,ProcessName,CPU | ConvertTo-Csv -NoTypeInformation'
    else:
        cmd = "ps aux --no-header | awk '{print $2, $11, $3}'"
    r = await _exec(cmd, timeout=10)
    if not r["ok"]:
        return r["stderr"]
    lines = r["stdout"].splitlines()
    if filter_name:
        lines = [l for l in lines if filter_name.lower() in l.lower()]
    return "\n".join(lines[:100]) or "（无匹配）"


@mcp.tool()
async def clipboard_read() -> str:
    """读取剪贴板内容"""
    if IS_WIN:
        r = await _exec("Get-Clipboard")
    else:
        r = await _exec("xclip -selection clipboard -o 2>/dev/null || xsel --clipboard --output 2>/dev/null || echo ''")
    return r["stdout"]


@mcp.tool()
async def clipboard_write(text: str) -> str:
    """写入剪贴板"""
    if IS_WIN:
        escaped = text.replace("'", "''")
        r = await _exec(f"Set-Clipboard -Value '{escaped}'")
    else:
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                ["xclip", "-selection", "clipboard"],
                input=text.encode(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            r = {"ok": completed.returncode == 0, "stderr": ""}
        except Exception:
            r = {"ok": False, "stderr": "xclip failed"}
    return "已写入剪贴板" if r["ok"] else f"失败: {r.get('stderr','')}"


# ════════════════════════════════════════════════════════
#  入口
# ════════════════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run(transport="stdio")

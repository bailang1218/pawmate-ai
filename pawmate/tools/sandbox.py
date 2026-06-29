"""
安全沙箱模块

提供安全的命令执行和文件操作环境
限制危险操作，防止恶意代码执行
"""
import asyncio
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Union
import shutil
from pawmate.core.resource_limits import read_binary_fileobj_limited
from pawmate.tools.unicode_debug import decode_subprocess_bytes, write_unicode_debug


class SecurityViolation(Exception):
    """安全违规异常"""
    pass


class CommandValidator:
    """命令验证器，确保命令的安全性"""

    # 危险命令黑名单（仅保留真正危险的操作）
    DANGEROUS_COMMANDS = {
        'dd', 'mkfs',
        'mount', 'umount', 'killall', 'pkill', 'shutdown',
        'halt', 'reboot', 'passwd', 'su', 'sudo', 'chsh', 'chfn',
        'usermod', 'userdel', 'groupmod', 'groupdel', 'adduser',
        'deluser', 'addgroup', 'delgroup', 'visudo', 'crontab',
        'at', 'batch',
    }

    # 安全命令白名单
    SAFE_COMMANDS = {
        'cat', 'grep', 'find', 'ls', 'pwd', 'echo', 'which', 'whereis',
        'man', 'info', 'help', 'ps', 'top', 'htop', 'free', 'df', 'du',
        'ping', 'wget', 'curl', 'nc', 'netstat', 'ss', 'arp', 'route',
        'traceroute', 'nslookup', 'dig', 'host', 'whoami', 'id', 'date',
        'time', 'uptime', 'last', 'history', 'jobs', 'bg', 'fg', 'kill',
        'sleep', 'yes', 'nohup', 'nice', 'renice', 'ionice', 'tac', 'sort',
        'uniq', 'wc', 'head', 'tail', 'cut', 'paste', 'join', 'diff',
        'cmp', 'comm', 'diff3', 'sdiff', 'patch', 'tr', 'col', 'expand',
        'unexpand', 'fmt', 'pr', 'fold', 'column', 'sha1sum', 'sha256sum',
        'md5sum', 'cksum', 'sum', 'basename', 'dirname', 'realpath', 'readlink',
        'touch', 'mkdir', 'rmdir', 'cp', 'mv', 'rm', 'ln', 'chown', 'chmod'
    }

    @classmethod
    def validate_command(cls, cmd: str) -> bool:
        """
        验证命令是否安全

        Args:
            cmd: 要验证的命令

        Returns:
            True 如果命令安全，否则抛出异常
        """
        # 分割命令以获取第一个词（通常是命令名）
        parts = cmd.strip().split()
        if not parts:
            return True

        command_name = parts[0].lower()

        # 检查命令是否在黑名单中
        if command_name in cls.DANGEROUS_COMMANDS:
            raise SecurityViolation(f"命令 '{command_name}' 被禁止执行")

        # 简单的路径遍历检测
        if '..' in cmd:
            if ('..' in cmd and
                (('/../' in cmd) or
                 (cmd.startswith('../')) or
                 ('/../' in cmd) or
                 (cmd.endswith('/..')) or
                 (' .. ' in cmd))):
                # 允许相对路径但限制向上遍历
                raise SecurityViolation("检测到潜在的路径遍历攻击")

        return True

    @classmethod
    def sanitize_path(cls, path: str) -> str:
        """
        清理和验证路径

        Args:
            path: 原始路径

        Returns:
            验证后的路径
        """
        try:
            from pawmate.core.path_security import (
                PathSecurityError,
                sanitize_path as sanitize_configured_path,
            )
            from pawmate.core.runtime_config import get_security_config

            return sanitize_configured_path(path, get_security_config())
        except PathSecurityError as exc:
            raise SecurityViolation(str(exc)) from exc

class Sandbox:
    """安全沙箱环境"""

    def __init__(self, max_runtime: int = 120, max_output_size: int = 1024 * 1024):
        """
        初始化沙箱

        Args:
            max_runtime: 最大运行时间（秒）
            max_output_size: 最大输出大小（字节）
        """
        self.max_runtime = max_runtime
        self.max_output_size = max_output_size
        self.is_win = platform.system() == "Windows"

    def _run_blocking_limited_output(
        self,
        args: List[str],
        cwd: Optional[str],
        timeout: int,
    ) -> tuple[int, bytes, bytes, bool, bool]:
        """Run a subprocess with stdout/stderr captured to temp files first."""
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            completed = subprocess.run(
                args,
                cwd=cwd,
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=timeout,
            )

            stdout_size = stdout_file.tell()
            stderr_size = stderr_file.tell()
            stdout_attr = getattr(completed, "stdout", None)
            stderr_attr = getattr(completed, "stderr", None)

            if stdout_size == 0 and isinstance(stdout_attr, bytes):
                stdout = stdout_attr[: self.max_output_size]
                stdout_truncated = len(stdout_attr) > self.max_output_size
            else:
                stdout, stdout_truncated = read_binary_fileobj_limited(
                    stdout_file,
                    self.max_output_size,
                )

            if stderr_size == 0 and isinstance(stderr_attr, bytes):
                stderr = stderr_attr[: self.max_output_size]
                stderr_truncated = len(stderr_attr) > self.max_output_size
            else:
                stderr, stderr_truncated = read_binary_fileobj_limited(
                    stderr_file,
                    self.max_output_size,
                )

            return completed.returncode, stdout, stderr, stdout_truncated, stderr_truncated

    async def _execute_prepared_command(
        self,
        args: List[str],
        cwd: Optional[str],
        timeout: int,
    ) -> Dict[str, Union[bool, int, str]]:
        try:
            returncode, stdout, stderr, stdout_truncated, stderr_truncated = await asyncio.to_thread(
                self._run_blocking_limited_output,
                args,
                cwd,
                timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "rc": -1,
                "stdout": "",
                "stderr": f"command timeout ({timeout}s)",
            }

        if (
            stdout_truncated
            or stderr_truncated
            or len(stdout) > self.max_output_size
            or len(stderr) > self.max_output_size
        ):
            return {
                "ok": False,
                "rc": -1,
                "stdout": "",
                "stderr": "output exceeded size limit",
            }

        write_unicode_debug("stage_06_raw_bytes", payload={
            "stdout_repr": repr(stdout[:200]),
            "stderr_repr": repr(stderr[:200]),
            "returncode": returncode,
        })

        stdout_text, _ = decode_subprocess_bytes(stdout)
        stderr_text, _ = decode_subprocess_bytes(stderr)

        write_unicode_debug("stage_07_decoded", payload={
            "stdout_repr": repr(stdout_text[:200]),
            "stderr_repr": repr(stderr_text[:200]),
        })
        return {
            "ok": returncode == 0,
            "rc": returncode,
            "stdout": stdout_text.strip(),
            "stderr": stderr_text.strip(),
        }

    async def _execute_prepared_script(
        self,
        runner: List[str],
        cwd: Optional[str],
        timeout: int,
    ) -> str:
        try:
            rc, stdout_b, stderr_b, stdout_truncated, stderr_truncated = await asyncio.to_thread(
                self._run_blocking_limited_output,
                runner,
                cwd,
                timeout,
            )
        except subprocess.TimeoutExpired:
            return f"[error] script timeout ({timeout}s)"
        except Exception as e:
            return f"[error] script execution failed: {str(e)}"

        if (
            stdout_truncated
            or stderr_truncated
            or len(stdout_b) > self.max_output_size
            or len(stderr_b) > self.max_output_size
        ):
            return "[error] output exceeded size limit"

        stdout, _ = decode_subprocess_bytes(stdout_b or b"")
        stderr, _ = decode_subprocess_bytes(stderr_b or b"")
        stdout = stdout.strip()
        stderr = stderr.strip()
        parts = [p for p in [stdout, f"[stderr] {stderr}" if stderr else ""] if p]
        if rc != 0:
            parts.append(f"[exit code {rc}]")
        return "\n".join(parts) if parts else "(no output)"

    async def execute_shell_command(
        self,
        cmd: str,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None
    ) -> Dict[str, Union[bool, int, str]]:
        """
        在沙箱中安全执行shell命令

        Args:
            cmd: 命令字符串
            cwd: 工作目录
            timeout: 超时时间

        Returns:
            执行结果字典
        """
        timeout = min(timeout or self.max_runtime, self.max_runtime)

        # 验证命令安全性（保留原有验证）
        CommandValidator.validate_command(cmd)

        # ── Sandbox mode 检查 ────────────────────────────────────
        allowed, reason = self._check_sandbox_mode(cmd)
        if not allowed:
            return {
                "ok": False,
                "rc": -1,
                "stdout": "",
                "stderr": f"[安全限制] {reason}",
            }

        # 确保工作目录在安全范围内
        if cwd:
            cwd = CommandValidator.sanitize_path(cwd)

        # 根据操作系统选择合适的命令执行方式
        if self.is_win:
            # PowerShell UTF-8 输出编码包装
            utf8_prefix = (
                "[Console]::OutputEncoding = "
                "[System.Text.UTF8Encoding]::new($false); "
                "$OutputEncoding = [Console]::OutputEncoding; "
            )
            args = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", utf8_prefix + cmd]
        else:
            args = ["bash", "-c", cmd]

        return await self._execute_prepared_command(args, cwd, timeout)

    async def execute_script(
        self,
        code: str,
        lang: str = "python",
        cwd: Optional[str] = None,
        timeout: Optional[int] = None
    ) -> str:
        """
        在沙箱中安全执行脚本

        Args:
            code: 代码内容
            lang: 语言类型
            cwd: 工作目录
            timeout: 超时时间

        Returns:
            执行结果
        """
        timeout = min(timeout or self.max_runtime, self.max_runtime)

        if cwd:
            cwd = CommandValidator.sanitize_path(cwd)

        # 验证语言类型
        supported_langs = {"python", "powershell", "bash", "node"}
        if lang not in supported_langs:
            return f"[错误] 不支持的语言: {lang}"

        # 限制代码长度
        if len(code) > 10000:  # 限制10KB
            return "[错误] 代码太长"

        suffix_map = {
            "python": ".py",
            "powershell": ".ps1",
            "bash": ".sh",
            "node": ".js"
        }
        suffix = suffix_map.get(lang, ".py")

        # 创建临时文件
        temp_fd, temp_path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
                f.write(code)

            # 构建执行命令
            if lang == "python":
                runner = [sys.executable, temp_path]
            elif lang == "powershell":
                runner = ["powershell", "-NoProfile", "-File", temp_path]
            elif lang == "bash":
                runner = ["bash", temp_path]
            elif lang == "node":
                runner = ["node", temp_path]
            else:
                return f"[错误] 不支持的语言: {lang}"

            return await self._execute_prepared_script(runner, cwd, timeout)
        except Exception as e:
            return f"[错误] 脚本执行失败: {str(e)}"
        finally:
            # 清理临时文件
            try:
                os.unlink(temp_path)
            except:
                pass

    @staticmethod
    def _check_sandbox_mode(cmd: str) -> tuple[bool, str]:
        """Check command against sandbox_mode from config."""
        try:
            from pawmate.core.sandbox import resolve_sandbox_mode, is_command_allowed
            from pawmate.core.runtime_config import load_runtime_config

            cfg = load_runtime_config()
            mode = resolve_sandbox_mode(cfg)
            allowed, reason = is_command_allowed(cmd, mode)
            import logging
            _logger = logging.getLogger("pawmate")
            _logger.info(
                "[Sandbox] mode=%s command=%r allowed=%s reason=%s",
                mode, cmd[:120], allowed, reason or "-",
            )
            if not allowed:
                msg = f"当前 sandbox_mode={mode}，已阻止该操作。{reason}"
                return False, msg
            return True, ""
        except Exception as exc:
            import logging
            logging.getLogger("pawmate").error(
                "[Sandbox] sandbox_mode check failed: %s; BLOCKING command", exc
            )
            return False, f"沙箱安全检查异常，已阻止执行: {exc}"

    def validate_file_path(self, path: str) -> str:
        """
        验证文件路径安全性

        Args:
            path: 文件路径

        Returns:
            验证后的路径
        """
        return CommandValidator.sanitize_path(path)

    def validate_directory_path(self, path: str, max_depth: int = 99) -> str:
        """
        验证目录路径安全性

        Args:
            path: 目录路径
            max_depth: 最大深度

        Returns:
            验证后的路径
        """
        path_obj = Path(path).resolve()
        home_dir = Path.home()

        try:
            rel_path = path_obj.relative_to(home_dir)
            depth = len(rel_path.parts)
            if depth > max_depth:
                raise SecurityViolation(f"目录深度超过限制 ({depth} > {max_depth})")
        except ValueError:
            pass

        return str(path_obj)

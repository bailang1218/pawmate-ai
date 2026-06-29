"""统一编码异常分析器 + 原子写入工具。

用于文件写入和 shell 调用前的字段级编码预检。
失败时返回结构化 EncodingIssue，不吞异常。
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class EncodingIssue:
    """编码异常详细信息。"""
    encoding: str
    field: str
    start: int
    end: int
    offending_text: str
    codepoints: list[str]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_encoding(
    text: str,
    *,
    encoding: str,
    field: str,
) -> Optional[EncodingIssue]:
    """
    在真正写文件或调用 subprocess 前进行严格编码预检。

    使用 errors="strict"：
    - 不静默删除异常字符
    - 不静默替换异常字符
    - 成功返回 None
    - 失败返回 EncodingIssue

    Args:
        text: 要检查的文本内容。
        encoding: 目标编码，如 "utf-8", "gbk"。
        field: 字段名称（用于定位错误来源）。

    Returns:
        None if 编码安全；EncodingIssue if 存在不可编码字符。
    """
    try:
        text.encode(encoding, errors="strict")
        return None
    except UnicodeEncodeError as exc:
        offending = exc.object[exc.start:exc.end]
        return EncodingIssue(
            encoding=encoding,
            field=field,
            start=exc.start,
            end=exc.end,
            offending_text=offending,
            codepoints=[f"U+{ord(ch):04X}" for ch in offending],
            message=str(exc),
        )


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """
    原子写入文本文件。

    关键要求：
    1. 写入前先完成编码（调用方保证）。
    2. 编码失败时不得触碰目标文件。
    3. 先写临时文件。
    4. 最后通过 os.replace 原子替换。
    """
    encoded = content.encode(encoding, errors="strict")

    tmp_path = path.with_name(f"{path.name}.tmp")

    try:
        tmp_path.write_bytes(encoded)
        os.replace(str(tmp_path), str(path))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def validate_shell_payload(
    *,
    command: str,
    cwd: Optional[str] = None,
    encoding: str = "gbk",
) -> Optional[dict]:
    """
    shell 命令执行前的多字段编码预检。

    逐个检查 command 和 cwd（如果提供），返回第一个发现的编码问题。

    Returns:
        None if all fields clean.
        dict with encoding_error details if any field fails.
    """
    cmd_issue = inspect_encoding(command, encoding=encoding, field="command")
    if cmd_issue is not None:
        return {
            "ok": False,
            "error_type": "encoding_error",
            "operation": "run_shell_command",
            "stage": "validate_payload",
            **cmd_issue.to_dict(),
        }

    if cwd:
        cwd_issue = inspect_encoding(cwd, encoding=encoding, field="cwd")
        if cwd_issue is not None:
            return {
                "ok": False,
                "error_type": "encoding_error",
                "operation": "run_shell_command",
                "stage": "validate_payload",
                **cwd_issue.to_dict(),
            }

    return None


def validate_batch_script(content: str) -> dict:
    """
    校验 .bat 脚本内容能否用 GBK 编码。
    禁止静默删除/替换异常字符。

    Returns:
        {"ok": True} if safe.
        {"ok": False, ...} with encoding_error details otherwise.
    """
    issue = inspect_encoding(content, encoding="gbk", field="script_content")
    if issue is not None:
        return {
            "ok": False,
            "error_type": "encoding_error",
            "operation": "generate_batch_script",
            "stage": "validate_script",
            **issue.to_dict(),
            "suggested_action": (
                "Regenerate an ASCII-only batch script or use PowerShell UTF-8."
            ),
        }
    return {"ok": True}

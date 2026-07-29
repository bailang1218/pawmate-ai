"""
Unicode 调试与安全解码工具。

专门处理 Windows 下 subprocess 输出解码问题。
禁止使用 errors="replace" 制造 U+FFFD。
"""
from __future__ import annotations

import json
import locale
import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger("pawmate")


def unicode_escape_text(value: Any) -> str:
    """生成不会因 Windows 控制台编码而崩溃的调试文本。"""
    return str(value).encode(
        "unicode_escape",
        errors="backslashreplace",
    ).decode("ascii", errors="strict")


def write_unicode_debug(
    stage: str,
    *,
    payload: dict[str, Any],
    log_dir: str | Path = "logs",
) -> None:
    """使用 UTF-8 文件记录阶段日志。日志失败不得影响业务。"""
    try:
        target = Path(log_dir) / "shell_unicode_debug.log"
        target.parent.mkdir(parents=True, exist_ok=True)

        safe_payload = {
            key: unicode_escape_text(value)
            for key, value in payload.items()
        }

        with target.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {"stage": stage, "payload": safe_payload},
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


def candidate_windows_decodings() -> Iterable[str]:
    """返回 Windows 下应尝试的解码编码列表。"""
    yield "utf-8"

    preferred = locale.getpreferredencoding(False)
    if preferred:
        yield preferred

    yield "gbk"
    yield "cp936"
    yield "mbcs"


def decode_subprocess_bytes(data: bytes) -> tuple[str, str]:
    """
    安全解码 subprocess 输出字节流。

    返回:
        (解码后的文本, 实际使用的编码)

    保证:
        - 不产生 U+FFFD
        - 优先严格 UTF-8
        - 失败后尝试系统代码页
        - 最终 fallback 使用 backslashreplace
    """
    if not data:
        return "", "empty"

    seen: set[str] = set()

    for encoding in candidate_windows_decodings():
        normalized = encoding.lower()
        if normalized in seen:
            continue
        seen.add(normalized)

        try:
            return data.decode(encoding, errors="strict"), encoding
        except (LookupError, UnicodeDecodeError):
            continue

    # Final fallback — never use "replace" (would create U+FFFD)
    text = data.decode("utf-8", errors="backslashreplace")
    return text, "utf-8-backslashreplace"


def console_safe_text(value: Any, *, limit: int = 300) -> str:
    """
    安全日志文本：不因 console 编码崩溃，不修改原始值。

    仅用于控制台日志/print 预览。
    """
    text = str(value)
    if len(text) > limit:
        text = text[:limit] + "..."

    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(enc, errors="backslashreplace").decode(enc, errors="strict")
    except Exception:
        return repr(text)

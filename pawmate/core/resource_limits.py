"""Shared resource limits for file, log, archive, and command output handling."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


MAX_TEXT_READ_BYTES = 2 * 1024 * 1024
MAX_LOG_TAIL_BYTES = 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024


@dataclass(slots=True)
class LimitedText:
    text: str
    truncated: bool
    size_bytes: int


def read_text_limited(
    path: str | Path,
    *,
    encoding: str = "utf-8",
    errors: str = "replace",
    max_bytes: int = MAX_TEXT_READ_BYTES,
) -> LimitedText:
    target = Path(path)
    size = target.stat().st_size
    with target.open("rb") as fp:
        data = fp.read(max_bytes + 1)
    truncated = len(data) > max_bytes or size > max_bytes
    if truncated:
        data = data[:max_bytes]
    return LimitedText(data.decode(encoding, errors=errors), truncated, size)


def read_tail_text(
    path: str | Path,
    *,
    encoding: str = "utf-8",
    errors: str = "replace",
    max_bytes: int = MAX_LOG_TAIL_BYTES,
) -> LimitedText:
    target = Path(path)
    size = target.stat().st_size
    with target.open("rb") as fp:
        if size > max_bytes:
            fp.seek(-max_bytes, 2)
            data = fp.read(max_bytes)
            truncated = True
        else:
            data = fp.read()
            truncated = False
    return LimitedText(data.decode(encoding, errors=errors), truncated, size)


def read_binary_fileobj_limited(fp: BinaryIO, max_bytes: int) -> tuple[bytes, bool]:
    fp.seek(0)
    data = fp.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return data, truncated

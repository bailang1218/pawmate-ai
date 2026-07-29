"""Low-noise logging for streaming text diagnostics."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Hashable


_SENTENCE_ENDINGS = ("。", "！", "？", ".", "!", "?", "\n")


@dataclass
class _TraceBuffer:
    component: str
    turn_id: int
    chunks: int = 0
    seq: int = 0
    chars: int = 0
    started_at: float = field(default_factory=time.monotonic)
    last_at: float = field(default_factory=time.monotonic)
    parts: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class StreamTraceAggregator:
    """Aggregate token-level stream deltas before writing diagnostic logs."""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        max_chars: int = 240,
        force_flush_chars: int = 180,
        force_flush_seconds: float = 2.0,
    ) -> None:
        self._logger = logger or logging.getLogger("pawmate")
        self._max_chars = max(40, int(max_chars or 240))
        self._force_flush_chars = max(40, int(force_flush_chars or 180))
        self._force_flush_seconds = max(0.2, float(force_flush_seconds or 2.0))
        self._buffers: dict[Hashable, _TraceBuffer] = {}

    def record(
        self,
        key: Hashable,
        *,
        component: str,
        turn_id: int,
        seq: int,
        text: str,
        **meta: Any,
    ) -> None:
        value = "" if text is None else str(text)
        if not value:
            return
        now = time.monotonic()
        buf = self._buffers.get(key)
        if buf is None:
            buf = _TraceBuffer(component=str(component), turn_id=int(turn_id or 0))
            self._buffers[key] = buf

        buf.component = str(component)
        buf.turn_id = int(turn_id or 0)
        buf.seq = int(seq or 0)
        buf.chunks += 1
        buf.chars += len(value)
        buf.last_at = now
        buf.meta = dict(meta)
        if value:
            buf.parts.append(value)

        if self._should_flush(buf, value, now):
            self.flush(key, reason=self._flush_reason(buf, value, now))

    def flush(self, key: Hashable, *, reason: str = "flush") -> None:
        buf = self._buffers.pop(key, None)
        if buf is None or (not buf.parts and buf.chunks == 0):
            return

        text = "".join(buf.parts)
        sample = text.replace("\r", "\\r").replace("\n", "\\n")
        if len(sample) > self._max_chars:
            sample = sample[: self._max_chars] + "..."
        meta = " ".join(f"{k}={v}" for k, v in sorted(buf.meta.items()))
        if meta:
            meta = " " + meta
        self._logger.info(
            "[StreamTrace] %s turn=%s seq=%s chunks=%s chars=%s reason=%s%s text=%r",
            buf.component,
            buf.turn_id,
            buf.seq,
            buf.chunks,
            buf.chars,
            reason,
            meta,
            sample,
        )

    def flush_matching(self, predicate, *, reason: str = "flush") -> None:
        keys = [key for key in self._buffers if predicate(key)]
        for key in keys:
            self.flush(key, reason=reason)

    def flush_all(self, *, reason: str = "flush") -> None:
        for key in list(self._buffers):
            self.flush(key, reason=reason)

    def _should_flush(self, buf: _TraceBuffer, value: str, now: float) -> bool:
        if not value:
            return False
        stripped = value.rstrip()
        if stripped.endswith(_SENTENCE_ENDINGS):
            return True
        if buf.chars >= self._force_flush_chars:
            return True
        return (now - buf.started_at) >= self._force_flush_seconds and buf.chars >= 40

    def _flush_reason(self, buf: _TraceBuffer, value: str, now: float) -> str:
        stripped = value.rstrip()
        if stripped.endswith(_SENTENCE_ENDINGS):
            return "sentence"
        if buf.chars >= self._force_flush_chars:
            return "size"
        if (now - buf.started_at) >= self._force_flush_seconds:
            return "time"
        return "flush"

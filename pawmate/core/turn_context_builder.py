from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any


class TurnContextBuilder:
    """Build the per-turn runtime prompt from read-only context sources."""

    def __init__(
        self,
        *,
        static_prompt_provider: Callable[[], str],
        conversation_context_provider: Callable[[], Any],
        long_term_memory_provider: Callable[[], Any],
        runtime_context_provider: Callable[[], str] | None = None,
    ) -> None:
        self._static_prompt_provider = static_prompt_provider
        self._conversation_context_provider = conversation_context_provider
        self._long_term_memory_provider = long_term_memory_provider
        self._runtime_context_provider = runtime_context_provider or self._default_runtime_context

    def compose_runtime_prompt(self, user_message: str = "") -> str:
        static = self._static_prompt_provider()
        conv_block = self._conversation_context_provider().build_context_block()
        memory = self._long_term_memory_provider()
        try:
            memory_block = memory.build_memory_block(user_message)
        except TypeError:
            memory_block = memory.build_memory_block()
        runtime_block = self._runtime_context_provider()
        parts = [static, conv_block, memory_block]
        if runtime_block:
            parts.append(runtime_block)
        return "\n".join(parts)

    @staticmethod
    def _default_runtime_context() -> str:
        return "## Runtime tail\n" f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

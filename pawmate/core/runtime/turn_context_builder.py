from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from pawmate.core.prompts.prompt_sections import (
    PromptSection,
    PromptSectionKind,
    TargetRole,
    TrustLevel,
    render_prompt_sections,
    trace_prompt_sections,
)


class TurnContextBuilder:
    """Build the per-turn runtime prompt from read-only context sources."""

    def __init__(
        self,
        *,
        static_prompt_provider: Callable[[], str],
        conversation_context_provider: Callable[[], Any],
        long_term_memory_provider: Callable[[], Any],
        runtime_context_provider: Callable[[], str] | None = None,
        skill_context_provider: Callable[[], str] | None = None,
    ) -> None:
        self._static_prompt_provider = static_prompt_provider
        self._conversation_context_provider = conversation_context_provider
        self._long_term_memory_provider = long_term_memory_provider
        self._runtime_context_provider = runtime_context_provider or self._default_runtime_context
        self._skill_context_provider = skill_context_provider or (lambda: "")
        self._last_trace: dict[str, Any] = {"turn_id": "", "sections": []}

    def compose_runtime_prompt(self, user_message: str = "", *, turn_id: str = "") -> str:
        sections = self.build_sections(user_message=user_message)
        self._last_trace = trace_prompt_sections(turn_id, sections)
        return render_prompt_sections(sections)

    def build_sections(self, user_message: str = "") -> list[PromptSection]:
        static = self._static_prompt_provider()
        conv_block = self._conversation_context_provider().build_context_block()
        memory = self._long_term_memory_provider()
        try:
            memory_block = memory.build_memory_block(user_message)
        except TypeError:
            memory_block = memory.build_memory_block()
        memory_trace_provider = getattr(memory, "get_last_injection_trace", None)
        memory_trace = memory_trace_provider() if callable(memory_trace_provider) else []
        runtime_block = self._runtime_context_provider()
        sections = [
            PromptSection(
                name="CORE_SYSTEM_RULES",
                kind=PromptSectionKind.SYSTEM_RULE,
                trust=TrustLevel.TRUSTED_INSTRUCTION,
                content=static,
                source="static_prompt_provider",
                target_role=TargetRole.SYSTEM,
                priority=10,
            ),
            PromptSection(
                name="DATA_ONLY_CONVERSATION_SUMMARY",
                kind=PromptSectionKind.SUMMARY_CONTEXT,
                trust=TrustLevel.MODEL_GENERATED,
                content=conv_block,
                source="conversation_context",
                target_role=TargetRole.SYSTEM,
                priority=60,
                data_only=True,
            ),
            PromptSection(
                name="DATA_ONLY_MEMORY_CONTEXT",
                kind=PromptSectionKind.MEMORY_CONTEXT,
                trust=TrustLevel.MODEL_GENERATED,
                content=memory_block,
                source="long_term_memory",
                target_role=TargetRole.SYSTEM,
                priority=70,
                data_only=True,
                metadata={"injection_trace": memory_trace},
            ),
            PromptSection(
                name="RUNTIME_METADATA",
                kind=PromptSectionKind.RUNTIME_METADATA,
                trust=TrustLevel.TRUSTED_RUNTIME,
                content=runtime_block,
                source="runtime_context_provider",
                target_role=TargetRole.SYSTEM,
                priority=90,
                data_only=True,
            ),
        ]
        skill_block = self._skill_context_provider()
        if skill_block:
            sections.append(
                PromptSection(
                    name="USER_APPROVED_SKILLS",
                    kind=PromptSectionKind.SKILL_INSTRUCTION,
                    trust=TrustLevel.TRUSTED_INSTRUCTION,
                    content=skill_block,
                    source="ready_skills",
                    target_role=TargetRole.SYSTEM,
                    priority=40,
                )
            )
        return sections

    def get_last_trace(self) -> dict[str, Any]:
        return {
            "turn_id": self._last_trace.get("turn_id", ""),
            "sections": list(self._last_trace.get("sections", [])),
        }

    @staticmethod
    def _default_runtime_context() -> str:
        return "## Runtime tail\n" f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

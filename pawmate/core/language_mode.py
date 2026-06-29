"""
Language mode configuration and system prompt builder.

Supports auto / zh-CN / en-US.

Refactored to delegate actual prompt assembly to PromptAssembler.
"""
from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pawmate.tools.registry import ToolRegistry

_logger = logging.getLogger("pawmate")

VALID_LANGUAGE_MODES = ("auto", "zh-CN", "en-US")
_DEFAULT_LANGUAGE_MODE = "auto"


def resolve_language_mode(config: dict) -> str:
    """Resolve language_mode from config, default to 'auto'.

    Args:
        config: Application configuration dict.

    Returns:
        One of 'auto', 'zh-CN', 'en-US'.
    """
    assistant_cfg = config.get("assistant", {})
    raw = assistant_cfg.get("language_mode", _DEFAULT_LANGUAGE_MODE)
    mode = str(raw).strip()
    if mode in VALID_LANGUAGE_MODES:
        return mode
    _logger.warning(
        "[Language] invalid language_mode=%r, falling back to 'auto'", raw,
    )
    return _DEFAULT_LANGUAGE_MODE


def build_system_prompt(config: dict, tool_registry: Optional[ToolRegistry] = None) -> str:
    """Build the full system prompt using PromptAssembler.

    Args:
        config: Application configuration dict.
        tool_registry: ToolRegistry with all registered tools. When omitted,
            a registry with built-in tools is created for backward compatibility.

    Returns:
        Complete system prompt string.
    """
    from pawmate.core.prompt_assembler import build_prompt_from_config
    if tool_registry is None:
        from pawmate.tools.registry import ToolRegistry
        from pawmate.tools.builtin_gateway import register_builtin_tools

        tool_registry = ToolRegistry()
        register_builtin_tools(tool_registry)

    prompt = build_prompt_from_config(config, tool_registry)
    _logger.debug("[Prompt] built system prompt (%d chars)", len(prompt))
    return prompt

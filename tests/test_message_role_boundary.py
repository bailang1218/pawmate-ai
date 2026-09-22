import importlib

import pytest

from pawmate.core.runtime.engine import AgentEngine
from pawmate.core.model.provider_runner import ProviderRunner
from pawmate.storage.history_store import HistoryStore


def test_provider_retry_continuation_does_not_create_user_message():
    messages = [{"role": "user", "content": "real user request"}]

    attempt_messages = ProviderRunner._messages_for_attempt(
        messages,
        "partial assistant answer",
        [],
    )

    assert [message["role"] for message in attempt_messages] == ["user", "assistant"]
    assert attempt_messages[0]["content"] == "real user request"
    assert "previous model provider failed" not in str(attempt_messages).lower()

    attempt_system = ProviderRunner._system_for_attempt(
        "core system prompt",
        "partial assistant answer",
        [],
    )
    assert "[Runtime continuation]" in attempt_system
    assert "previous model provider failed" in attempt_system.lower()


def test_history_store_rejects_tool_result_without_id():
    store = object.__new__(HistoryStore)

    with pytest.raises(ValueError, match="tool_call_id"):
        store.append_tool_result("", "result")


def test_engine_pending_tool_result_rejects_missing_id():
    engine = object.__new__(AgentEngine)
    pending = []

    with pytest.raises(ValueError, match="tool_call_id"):
        engine._queue_pending_tool_result(pending, "", "read_text_file", "result")

    assert pending == []


def _provider_classes():
    modules = [
        ("pawmate.core.model.providers.openai_provider", "OpenAIProvider"),
        ("pawmate.core.model.providers.deepseek_provider", "DeepSeekProvider"),
        ("pawmate.core.model.providers.qwen_provider", "QwenProvider"),
        ("pawmate.core.model.providers.gemini_provider", "GeminiProvider"),
        ("pawmate.core.model.providers.minimaxi_provider", "MinimaxiProvider"),
        ("pawmate.core.model.providers.anthropic_provider", "AnthropicProvider"),
    ]
    classes = []
    for module_name, class_name in modules:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if module_name.endswith("anthropic_provider") and exc.name == "anthropic":
                continue
            raise
        classes.append(getattr(module, class_name))
    return classes


def test_provider_formatters_reject_tool_result_without_id():
    tool_message = {
        "role": "tool",
        "content": "file content",
        "tool_name": "read_text_file",
        "tool_use_id": "",
    }

    for provider_class in _provider_classes():
        provider = object.__new__(provider_class)
        with pytest.raises(ValueError, match="tool_call_id"):
            provider.format_messages([tool_message])


def test_openai_compatible_formatter_preserves_valid_tool_call_id():
    module = importlib.import_module("pawmate.core.model.providers.openai_provider")
    provider = object.__new__(module.OpenAIProvider)

    formatted = provider.format_messages([
        {
            "role": "tool",
            "content": "file content",
            "tool_name": "read_text_file",
            "tool_use_id": "call_123",
        }
    ])

    assert formatted == [
        {
            "role": "tool",
            "tool_call_id": "call_123",
            "content": "file content",
        }
    ]

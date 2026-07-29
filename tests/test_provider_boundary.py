from types import SimpleNamespace

import pytest

from pawmate.core.model.provider_contract import normalize_provider_stream_event
from pawmate.core.model.provider_runner import ProviderRunner
from pawmate.core.model.providers.deepseek_provider import DeepSeekProvider
from pawmate.core.model.providers.gemini_provider import GeminiProvider
from pawmate.core.model.providers.openai_provider import OpenAIProvider
from pawmate.core.model.providers.qwen_provider import QwenProvider


def test_provider_contract_rejects_malformed_tool_use_events():
    with pytest.raises(ValueError, match="tool_call_id"):
        normalize_provider_stream_event({"type": "tool_use", "id": "", "name": "read", "input": {}})

    with pytest.raises(ValueError, match="tool name"):
        normalize_provider_stream_event({"type": "tool_use", "id": "call_1", "name": "", "input": {}})

    with pytest.raises(ValueError, match="input must be a dict"):
        normalize_provider_stream_event({"type": "tool_use", "id": "call_1", "name": "read", "input": "raw"})


@pytest.mark.asyncio
async def test_provider_runner_normalizes_stream_events_before_runtime_consumes_them():
    class BadProvider:
        async def stream(self, **_kwargs):
            yield {"type": "tool_use", "id": "", "name": "read_text_file", "input": {}}

        def get_last_response(self):
            return None

    runner = ProviderRunner(BadProvider(), current_provider="bad", fallback_providers=["bad"])

    with pytest.raises(ValueError, match="tool_call_id"):
        await runner.collect_stream_events(
            messages=[],
            tools_defs=[],
            final_response="",
            system_prompt_factory=lambda: "system",
        )


@pytest.mark.asyncio
async def test_provider_runner_emits_fallback_status_without_polluting_text():
    class FailingProvider:
        async def stream(self, **_kwargs):
            if False:
                yield {"type": "text_delta", "text": ""}
            raise RuntimeError("primary unavailable")

        def get_last_response(self):
            return None

    class GoodProvider:
        async def stream(self, **_kwargs):
            yield {"type": "text_delta", "text": "ok"}

        def get_last_response(self):
            return None

    statuses = []

    def factory(provider, **_kwargs):
        assert provider == "good"
        return GoodProvider()

    runner = ProviderRunner(
        FailingProvider(),
        current_provider="bad",
        fallback_providers=["good"],
        client_factory=factory,
        status_callback=statuses.append,
    )

    current_text, final_response, _events = await runner.collect_stream_events(
        messages=[{"role": "user", "content": "hello"}],
        tools_defs=[],
        final_response="",
        system_prompt_factory=lambda: "system",
    )

    assert current_text == "ok"
    assert final_response == "ok"
    assert "\u5207\u6362\u5230" not in final_response
    assert statuses
    assert statuses[0]["event"] == "provider_fallback_switch"
    assert statuses[0]["provider"] == "good"
    assert statuses[0]["after"] == ["bad"]
    assert statuses[0]["context_inherited"] is True
    assert statuses[0]["message_count"] == 1
    assert statuses[0]["reason_type"] == "RuntimeError"


def test_deepseek_preserves_reasoning_content_for_thinking_mode_replay():
    provider = object.__new__(DeepSeekProvider)

    formatted = provider.format_messages([
        {
            "role": "assistant",
            "content": {
                "text": "visible answer",
                "reasoning_content": "hidden thinking state",
            },
        }
    ])

    assert formatted == [
        {
            "role": "assistant",
            "content": "visible answer",
            "reasoning_content": "hidden thinking state",
        }
    ]


def test_non_deepseek_providers_strip_reasoning_content_from_history():
    provider = object.__new__(QwenProvider)

    formatted = provider.format_messages([
        {
            "role": "assistant",
            "content": {
                "text": "visible answer",
                "reasoning_content": "hidden thinking state",
            },
        }
    ])

    assert formatted == [{"role": "assistant", "content": "visible answer"}]


def test_openai_streaming_tool_calls_are_aggregated_by_index_and_preserve_ids():
    pending = {}

    OpenAIProvider._accumulate_tool_call(
        pending,
        SimpleNamespace(
            index=1,
            id="call_b",
            function=SimpleNamespace(name="write_file", arguments='{"path"'),
        ),
    )
    OpenAIProvider._accumulate_tool_call(
        pending,
        SimpleNamespace(
            index=0,
            id="call_a",
            function=SimpleNamespace(name="read_text_file", arguments='{"path":'),
        ),
    )
    OpenAIProvider._accumulate_tool_call(
        pending,
        SimpleNamespace(index=1, id=None, function=SimpleNamespace(name=None, arguments=':"b.txt"}')),
    )
    OpenAIProvider._accumulate_tool_call(
        pending,
        SimpleNamespace(index=0, id=None, function=SimpleNamespace(name=None, arguments='"README.md"}')),
    )

    provider = object.__new__(OpenAIProvider)
    events = list(provider._flush_tool_calls(pending))

    assert [event["id"] for event in events] == ["call_a", "call_b"]
    assert [event["name"] for event in events] == ["read_text_file", "write_file"]
    assert events[0]["input"] == {"path": "README.md"}
    assert events[1]["input"] == {"path": "b.txt"}
    assert pending == {}


def test_openai_compatible_flush_rejects_missing_provider_tool_call_id():
    providers = [
        object.__new__(OpenAIProvider),
        object.__new__(DeepSeekProvider),
        object.__new__(QwenProvider),
        object.__new__(GeminiProvider),
    ]

    for provider in providers:
        with pytest.raises(ValueError, match="tool_call_id"):
            list(provider._flush_tool_calls({
                0: {"id": "", "name": "read_text_file", "arguments": ["{}"]},
            }))

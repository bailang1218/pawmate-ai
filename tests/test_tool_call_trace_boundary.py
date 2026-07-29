from types import SimpleNamespace

import httpx
import pytest

from pawmate.core.runtime.engine import AgentEngine
from pawmate.core.model.provider_runner import ProviderRunner
from pawmate.core.tools.tool_policy import PolicyDecision
from pawmate.core.observability.trace import AgentRunTrace


def test_agent_run_trace_redacts_secrets_and_exports_json():
    trace = AgentRunTrace("run_1", trace_id="trace_1")

    trace.add_event(
        "tool_execution_started",
        turn_id="turn_1",
        tool_call_id="call_1",
        data={"api_key": "sk-secret-value", "nested": {"password": "pw"}},
    )

    payload = trace.to_dict()
    event_data = payload["events"][0]["data"]
    assert event_data["api_key"] == "[REDACTED]"
    assert event_data["nested"]["password"] == "[REDACTED]"
    assert "sk-secret-value" not in trace.export_json()


@pytest.mark.asyncio
async def test_provider_runner_records_retry_attempt_trace():
    class FlakyProvider:
        def __init__(self):
            self.calls = 0

        async def stream(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError("temporary network break")
            yield {"type": "text_delta", "text": "ok"}

        def get_last_response(self):
            return SimpleNamespace(content="ok")

    provider = FlakyProvider()
    runner = ProviderRunner(
        provider,
        current_provider="fake",
        fallback_providers=["fake"],
        client_factory=lambda **_kwargs: provider,
    )

    current_text, final_response, stream_events = await runner.collect_stream_events(
        messages=[{"role": "user", "content": "hi"}],
        tools_defs=[],
        final_response="",
        system_prompt_factory=lambda: "system",
    )

    assert current_text == "ok"
    assert final_response == "ok"
    assert stream_events == []
    attempt_trace = runner.get_last_attempt_trace()
    assert [item["event"] for item in attempt_trace] == [
        "provider_attempt_started",
        "provider_attempt_error",
        "provider_attempt_started",
        "provider_attempt_succeeded",
    ]
    assert attempt_trace[1]["error_type"] == "ConnectError"


def test_engine_records_policy_confirm_and_observation_trace_events():
    engine = object.__new__(AgentEngine)
    engine._trace = AgentRunTrace("run_1", trace_id="trace_1")
    engine._registry = SimpleNamespace(
        _last_policy_decision=PolicyDecision(
            allowed=True,
            reason="requires_confirmation",
            requires_confirm=True,
            risk="high",
            side_effect="destructive",
        ),
        _last_approval=SimpleNamespace(allowed=False, reason="denied"),
        get_tool=lambda name: SimpleNamespace(name=name, approval="confirm"),
    )
    views = SimpleNamespace(
        model="model",
        ui="ui",
        truncated=True,
        raw_ref="C:/tmp/result.txt",
        original_chars=9000,
    )

    engine._record_policy_and_confirm_trace("call_1", "delete_path")
    engine._record_tool_trace_finished(
        "call_1",
        "delete_path",
        views,
        success=False,
        elapsed=0.1,
        error="denied",
    )

    events = engine.get_agent_run_trace()["events"]
    event_types = [event["event_type"] for event in events]
    assert event_types == [
        "policy_decision",
        "confirm_decision",
        "tool_execution_finished",
        "observation",
    ]
    assert events[0]["data"]["requires_confirm"] is True
    assert events[1]["data"]["reason"] == "denied"
    assert events[3]["data"]["truncated"] is True
    assert events[3]["tool_call_id"] == "call_1"


def test_engine_trace_export_is_available_even_for_lightweight_instances():
    engine = object.__new__(AgentEngine)
    engine._trace = AgentRunTrace("run_1", trace_id="trace_1")

    engine._record_trace_event("loop_decision", data={"status": "COMPLETED"})

    exported = engine.export_agent_run_trace_json()
    assert "trace_1" in exported
    assert "loop_decision" in exported

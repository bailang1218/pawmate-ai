import pytest

from pawmate.core.runtime.engine import AgentEngine
from pawmate.core.tools.tool_call_lifecycle import (
    RuntimeToolCall,
    ToolCallStatus,
    reject_duplicate_tool_call_id,
)


def test_runtime_tool_call_from_provider_event_records_parse_and_validation_trace():
    call = RuntimeToolCall.from_provider_event(
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "read_text_file",
            "input": {"path": "README.md"},
            "provider": "openai",
        },
        turn_id="turn-1",
    )

    assert call.id == "call_1"
    assert call.name == "read_text_file"
    assert call.arguments == {"path": "README.md"}
    assert call.status == ToolCallStatus.VALIDATED
    assert [item["status"] for item in call.trace] == [
        "created_by_model",
        "parsed",
        "validated",
    ]
    assert call.to_assistant_tool_call() == {
        "id": "call_1",
        "name": "read_text_file",
        "input": {"path": "README.md"},
    }


def test_runtime_tool_call_rejects_missing_id_and_invalid_arguments():
    with pytest.raises(ValueError, match="tool_call_id"):
        RuntimeToolCall.from_provider_event({"id": "", "name": "read", "input": {}})

    with pytest.raises(ValueError, match="arguments must be a dict"):
        RuntimeToolCall.from_provider_event({"id": "call_1", "name": "read", "input": "raw"})


def test_runtime_tool_call_rejects_invalid_status_transition():
    call = RuntimeToolCall.from_provider_event({"id": "call_1", "name": "read", "input": {}})

    with pytest.raises(ValueError, match="invalid tool_call status transition"):
        call.transition(ToolCallStatus.OBSERVED)


def test_duplicate_tool_call_id_is_hard_fail():
    seen = set()

    reject_duplicate_tool_call_id("call_1", seen)

    with pytest.raises(ValueError, match="duplicate tool_call_id"):
        reject_duplicate_tool_call_id("call_1", seen)


def test_engine_builds_runtime_tool_call_request_and_rejects_duplicate_ids():
    engine = object.__new__(AgentEngine)
    seen = set()

    call = engine._build_runtime_tool_call_request(
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "read_text_file",
            "input": {"path": "README.md"},
            "provider": "qwen",
        },
        seen,
    )

    assert call.id == "call_1"
    assert call.source_provider == "qwen"
    assert seen == {"call_1"}
    with pytest.raises(ValueError, match="duplicate tool_call_id"):
        engine._build_runtime_tool_call_request(
            {"type": "tool_use", "id": "call_1", "name": "other", "input": {}},
            seen,
        )


def test_engine_marks_runtime_tool_call_observed_after_success_or_failure():
    success_call = RuntimeToolCall.from_provider_event({"id": "call_1", "name": "read", "input": {}})
    AgentEngine._mark_runtime_tool_call_executing(success_call)
    AgentEngine._finish_runtime_tool_call(success_call, success=True)

    assert success_call.status == ToolCallStatus.OBSERVED
    assert [item["status"] for item in success_call.trace][-3:] == [
        "executing",
        "succeeded",
        "observed",
    ]

    failed_call = RuntimeToolCall.from_provider_event({"id": "call_2", "name": "read", "input": {}})
    AgentEngine._mark_runtime_tool_call_executing(failed_call)
    AgentEngine._finish_runtime_tool_call(failed_call, success=False)

    assert failed_call.status == ToolCallStatus.OBSERVED
    assert [item["status"] for item in failed_call.trace][-3:] == [
        "executing",
        "failed",
        "observed",
    ]

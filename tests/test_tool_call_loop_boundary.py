import pytest
import json

from pawmate.core.runtime.engine import AgentEngine
from pawmate.core.runtime.loop_control import (
    CompletionStatus,
    LoopController,
    RecoveryErrorType,
)
from pawmate.core.tools.tool_call_lifecycle import RuntimeToolCall, ToolCallStatus
from pawmate.core.tools.tool_observation import wrap_tool_observation_for_model


def test_loop_controller_enforces_max_iterations():
    controller = LoopController(max_iterations=1, max_tool_calls=10)

    first = controller.before_iteration()
    second = controller.before_iteration()

    assert first.should_continue is True
    assert first.completion_status == CompletionStatus.RUNNING
    assert first.iteration == 1
    assert second.should_continue is False
    assert second.completion_status == CompletionStatus.BUDGET_EXCEEDED
    assert second.reason == "max_iterations exceeded"


def test_tool_finished_does_not_mark_task_finished():
    controller = LoopController(max_iterations=5, max_tool_calls=5)
    controller.before_iteration()

    decision = controller.record_tool_call(
        "read_text_file",
        {"path": "README.md"},
        success=True,
    )

    assert decision.should_continue is True
    assert decision.completion_status == CompletionStatus.RUNNING
    assert decision.reason == "tool_finished_task_still_running"


def test_loop_controller_stops_on_repeated_identical_actions():
    controller = LoopController(
        max_iterations=5,
        max_tool_calls=10,
        repeated_action_stop_threshold=2,
    )
    controller.before_iteration()

    first = controller.record_tool_call("browser_read", {"selector": "#x"}, success=True)
    second = controller.record_tool_call("browser_read", {"selector": "#x"}, success=True)

    assert first.should_continue is True
    assert second.should_continue is False
    assert second.completion_status == CompletionStatus.PARTIALLY_COMPLETED
    assert second.error_type == RecoveryErrorType.REPEATED_ACTION
    assert second.repeated_action_count == 2


def test_loop_controller_does_not_accumulate_interleaved_actions_as_a_loop():
    controller = LoopController(
        max_iterations=8,
        max_tool_calls=10,
        repeated_action_stop_threshold=2,
    )
    controller.before_iteration()

    controller.record_tool_call("browser_read", {"selector": "#x"}, success=True)
    controller.record_tool_call("browser_act", {"ref": "button-1"}, success=True)
    decision = controller.record_tool_call("browser_read", {"selector": "#x"}, success=True)

    assert decision.should_continue is True
    assert decision.repeated_action_count == 1


def test_loop_controller_treats_changed_observation_as_progress():
    controller = LoopController(
        max_iterations=8,
        max_tool_calls=10,
        repeated_action_stop_threshold=2,
    )
    controller.before_iteration()

    first = controller.record_tool_call(
        "browser_read",
        {"selector": "#x"},
        success=True,
        observation_fingerprint="frame-1",
    )
    progressed = controller.record_tool_call(
        "browser_read",
        {"selector": "#x"},
        success=True,
        observation_fingerprint="frame-2",
    )
    stalled = controller.record_tool_call(
        "browser_read",
        {"selector": "#x"},
        success=True,
        observation_fingerprint="frame-2",
    )

    assert first.should_continue is True
    assert progressed.should_continue is True
    assert stalled.should_continue is False


def test_loop_controller_zero_runtime_limit_disables_hidden_five_minute_stop():
    controller = LoopController(
        max_iterations=8,
        max_tool_calls=10,
        max_runtime_seconds=0,
    )
    controller.started_at -= 3600

    decision = controller.before_iteration()

    assert decision.should_continue is True
    assert controller.max_runtime_seconds is None


def test_loop_controller_stops_on_consecutive_tool_failures():
    controller = LoopController(
        max_iterations=5,
        max_tool_calls=10,
        failure_stop_threshold=2,
    )
    controller.before_iteration()

    first = controller.record_tool_call("read_text_file", {"path": "missing"}, success=False)
    second = controller.record_tool_call("read_text_file", {"path": "missing-2"}, success=False)

    assert first.should_continue is True
    assert second.should_continue is False
    assert second.completion_status == CompletionStatus.FAILED
    assert second.error_type == RecoveryErrorType.TOOL_EXCEPTION
    assert second.failure_count == 2


def test_loop_controller_uses_explicit_confirmation_pause_status():
    controller = LoopController(max_iterations=5, max_tool_calls=5)

    decision = controller.pause_for_confirmation("delete_path needs approval")

    assert decision.should_continue is False
    assert decision.completion_status == CompletionStatus.NEEDS_CONFIRMATION
    assert decision.human_intervention_required is True


def test_engine_runtime_tool_call_result_success_is_recorded_after_finish():
    call = RuntimeToolCall.from_provider_event({
        "id": "call_1",
        "name": "read_text_file",
        "input": {"path": "README.md"},
    })

    AgentEngine._mark_runtime_tool_call_executing(call)
    AgentEngine._finish_runtime_tool_call(call, success=True)

    assert call.result_success is True
    assert call.status == ToolCallStatus.OBSERVED


def test_loop_controller_rejects_running_as_completion_status():
    controller = LoopController(max_iterations=5, max_tool_calls=5)

    with pytest.raises(ValueError, match="cannot be RUNNING"):
        controller.complete(CompletionStatus.RUNNING)


def test_engine_reads_zero_task_timeout_as_disabled():
    engine = AgentEngine.__new__(AgentEngine)
    engine._app_config = {"runtime": {"task_timeout_seconds": 0}}

    assert engine._get_task_timeout_seconds() == 0.0


def test_engine_appends_missing_search_source_links_to_answer():
    response = AgentEngine._append_web_evidence_sources(
        "已核实。",
        [
            {"title": "Source A", "url": "https://example.com/a"},
            {"title": "Duplicate", "url": "https://example.com/a"},
        ],
    )

    assert "依据来源" in response
    assert response.count("https://example.com/a") == 1


def test_engine_extracts_only_grounded_sources_from_tool_observation():
    payload = json.dumps({
        "ok": True,
        "grounded": True,
        "sources": [{"title": "Official", "url": "https://official.example/report"}],
    })
    pending = [{
        "tool_use_id": "call-search",
        "tool_name": "native_web_search",
        "result": wrap_tool_observation_for_model(
            tool_call_id="call-search",
            tool_name="native_web_search",
            content=payload,
        ),
    }]

    sources = AgentEngine._pending_web_evidence_sources(pending, "call-search")

    assert sources == [{"title": "Official", "url": "https://official.example/report"}]

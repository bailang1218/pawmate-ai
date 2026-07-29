from types import SimpleNamespace

import pytest

from pawmate.core.checkpoint.checkpoint import AgentCheckpoint, CheckpointStage, CheckpointStore
from pawmate.core.runtime.engine import AgentEngine, EngineState
from pawmate.core.runtime.runtime_state import AgentRunState
from pawmate.core.tools.tool_call_lifecycle import RuntimeToolCall
from pawmate.core.tools.tool_call_runner import ToolCallOutcome
from pawmate.core.tools.tool_result_budget import ToolResultViews
from pawmate.core.observability.trace import AgentRunTrace


def test_checkpoint_store_saves_redacted_latest_checkpoint(tmp_path):
    store = CheckpointStore(root=tmp_path)
    checkpoint = AgentCheckpoint.create(
        run_id="run_1",
        turn_id="turn_1",
        stage=CheckpointStage.BEFORE_TOOL_EXECUTION,
        tool_call_id="call_1",
        tool_name="write_file",
        side_effect="local_write",
        state={"tool_input": {"path": "x", "api_key": "sk-secret-token"}},
    )

    path = store.save(checkpoint)
    latest = store.load_latest("run_1")

    assert path.exists()
    assert latest is not None
    assert latest.stage == CheckpointStage.BEFORE_TOOL_EXECUTION
    assert latest.state["tool_input"]["api_key"] == "[REDACTED]"
    assert latest.replay_allowed is False
    assert "sk-secret-token" not in path.read_text(encoding="utf-8")


def test_checkpoint_store_lists_checkpoints_in_order(tmp_path):
    store = CheckpointStore(root=tmp_path)
    first = AgentCheckpoint.create(
        run_id="run_1",
        turn_id="turn_1",
        stage=CheckpointStage.TURN_STARTED,
    )
    second = AgentCheckpoint.create(
        run_id="run_1",
        turn_id="turn_1",
        stage=CheckpointStage.TURN_FINISHED,
    )

    store.save(first)
    store.save(second)

    checkpoints = store.list_checkpoints("run_1")
    assert [checkpoint.stage for checkpoint in checkpoints] == [
        CheckpointStage.TURN_STARTED,
        CheckpointStage.TURN_FINISHED,
    ]


def test_engine_save_checkpoint_records_trace_event(tmp_path):
    engine = object.__new__(AgentEngine)
    engine._run_state = AgentRunState(run_id="run_1")
    engine._trace = AgentRunTrace("run_1", trace_id="trace_1")
    engine._checkpoint_store = CheckpointStore(root=tmp_path)

    engine._save_checkpoint(
        CheckpointStage.PROVIDER_ERROR,
        state={"error": "provider failed", "password": "pw"},
    )

    latest = engine.get_latest_checkpoint()
    assert latest["stage"] == CheckpointStage.PROVIDER_ERROR.value
    assert latest["state"]["password"] == "[REDACTED]"
    assert latest["replay_allowed"] is False
    assert engine.get_agent_run_trace()["events"][0]["event_type"] == "checkpoint"


@pytest.mark.asyncio
async def test_engine_tool_execution_writes_confirm_before_and_after_checkpoints(tmp_path):
    engine = object.__new__(AgentEngine)
    engine._run_state = AgentRunState(run_id="run_1")
    engine._trace = AgentRunTrace("run_1", trace_id="trace_1")
    engine._checkpoint_store = CheckpointStore(root=tmp_path)
    engine._state = EngineState(tool_call_history=[])
    engine._history = SimpleNamespace(session_id="session_1")
    engine._tool_failure_streak = {}
    engine._registry = SimpleNamespace(
        _last_policy_decision=None,
        _last_approval=SimpleNamespace(allowed=True, reason="approved"),
        get_tool=lambda name: SimpleNamespace(
            name=name,
            approval="confirm",
            side_effect="destructive",
            description="delete path",
        ),
    )

    async def fake_run(*args, **kwargs):
        return ToolCallOutcome(
            success=True,
            views=ToolResultViews(
                ui="ok",
                model="ok",
                log="ok",
                raw_ref=None,
                truncated=False,
                original_chars=2,
            ),
            elapsed=0.01,
        )

    engine._tool_call_runner = SimpleNamespace(
        run=fake_run
    )
    runtime_call = RuntimeToolCall.from_provider_event({
        "id": "call_1",
        "name": "delete_path",
        "input": {"path": "tmp.txt"},
    })
    pending: list[dict[str, str]] = []

    await engine._handle_tool_call_event(
        "delete_path",
        {"path": "tmp.txt"},
        "call_1",
        pending,
        runtime_tool_call=runtime_call,
    )

    checkpoints = engine._checkpoint_store.list_checkpoints("run_1")
    stages = [checkpoint.stage for checkpoint in checkpoints]
    assert CheckpointStage.CONFIRM_PENDING in stages
    assert CheckpointStage.BEFORE_TOOL_EXECUTION in stages
    assert CheckpointStage.AFTER_TOOL_EXECUTION in stages
    assert checkpoints[-1].tool_call_id == "call_1"
    assert checkpoints[-1].side_effect == "destructive"
    assert checkpoints[-1].replay_allowed is False
    assert pending[0]["tool_use_id"] == "call_1"


@pytest.mark.asyncio
async def test_engine_treats_ok_false_tool_payload_as_failed_evidence(tmp_path):
    engine = object.__new__(AgentEngine)
    engine._run_state = AgentRunState(run_id="run_1")
    engine._trace = AgentRunTrace("run_1", trace_id="trace_1")
    engine._checkpoint_store = CheckpointStore(root=tmp_path)
    engine._state = EngineState(tool_call_history=[])
    engine._history = SimpleNamespace(session_id="session_1")
    engine._tool_failure_streak = {}
    engine._registry = SimpleNamespace(
        _last_policy_decision=None,
        _last_approval=None,
        get_tool=lambda name: SimpleNamespace(
            name=name,
            approval="notify",
            side_effect="read_only",
            description="web search",
            tags=[],
        ),
    )

    async def fake_run(*args, **kwargs):
        payload = '{"ok": false, "error_type": "all_providers_failed"}'
        return ToolCallOutcome(
            success=True,
            views=ToolResultViews(
                ui=payload,
                model=payload,
                log=payload,
                raw_ref=None,
                truncated=False,
                original_chars=len(payload),
            ),
            elapsed=0.01,
        )

    engine._tool_call_runner = SimpleNamespace(run=fake_run)
    runtime_call = RuntimeToolCall.from_provider_event({
        "id": "call_search",
        "name": "native_web_search",
        "input": {"query": "current fact"},
    })
    pending: list[dict[str, str]] = []

    visible_result = await engine._handle_tool_call_event(
        "native_web_search",
        {"query": "current fact"},
        "call_search",
        pending,
        runtime_tool_call=runtime_call,
    )

    assert visible_result is None
    assert runtime_call.result_success is False
    assert engine._state.tool_call_history[-1]["success"] is False
    assert engine._checkpoint_store.list_checkpoints("run_1")[-1].state["success"] is False

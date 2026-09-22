import asyncio
import types

import pytest

from pawmate.core.runtime.engine import AgentEngine
from pawmate.core.runtime.runtime_state import (
    AgentRunState,
    ResourceKey,
    ResourceKind,
    ResourceLockManager,
    infer_tool_resources,
)
from pawmate.core.tools.tool_call_lifecycle import RuntimeToolCall


def test_agent_run_state_records_turn_and_failure_recovery_state():
    state = AgentRunState(run_id="run_test")

    turn = state.start_turn("turn_1")
    turn.user_seq = 42
    state.finish_turn("failed", "provider_error")

    snapshot = state.to_dict()
    assert snapshot["run_id"] == "run_test"
    assert snapshot["last_status"] == "failed"
    assert snapshot["last_error"] == "provider_error"
    assert snapshot["current_turn"]["turn_id"] == "turn_1"
    assert snapshot["current_turn"]["user_seq"] == 42


def test_runtime_tool_call_preserves_run_and_turn_ids_without_provider_payload_pollution():
    call = RuntimeToolCall.from_provider_event(
        {
            "id": "call_1",
            "name": "read_text_file",
            "input": {"path": "README.md"},
            "provider": "openai",
        },
        run_id="run_1",
        turn_id="turn_1",
    )

    assert call.run_id == "run_1"
    assert call.turn_id == "turn_1"
    assert call.to_tool_call_request()["run_id"] == "run_1"
    assert call.to_tool_call_request()["turn_id"] == "turn_1"
    assert call.to_assistant_tool_call() == {
        "id": "call_1",
        "name": "read_text_file",
        "input": {"path": "README.md"},
    }


def test_infer_tool_resources_marks_browser_file_shell_memory_and_network_boundaries():
    assert infer_tool_resources("browser_read", {}) == [
        ResourceKey(ResourceKind.BROWSER_SESSION.value, "active")
    ]
    assert infer_tool_resources("run_shell_command", {"cwd": "repo"}) == [
        ResourceKey(ResourceKind.SHELL_PROCESS.value, "repo")
    ]
    assert infer_tool_resources("search_memory", {}) == [
        ResourceKey(ResourceKind.MEMORY_STORE.value, "local")
    ]
    assert infer_tool_resources("open_url", {"url": "https://example.com"}) == [
        ResourceKey(ResourceKind.NETWORK_SESSION.value, "external")
    ]

    file_resources = infer_tool_resources("write_file", {"path": "README.md"})
    assert len(file_resources) == 1
    assert file_resources[0].kind == ResourceKind.FILE_PATH.value
    assert file_resources[0].name.endswith("README.md")


@pytest.mark.asyncio
async def test_resource_lock_manager_serializes_same_resource():
    manager = ResourceLockManager()
    resource = ResourceKey(ResourceKind.FILE_PATH.value, "same-file")
    entered: list[str] = []
    first_entered = asyncio.Event()
    allow_first_exit = asyncio.Event()

    async def first():
        async with manager.hold_many([resource], owner="first"):
            entered.append("first")
            first_entered.set()
            await allow_first_exit.wait()
            entered.append("first_exit")

    async def second():
        await first_entered.wait()
        async with manager.hold_many([resource], owner="second"):
            entered.append("second")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())

    await first_entered.wait()
    await asyncio.sleep(0.05)

    assert entered == ["first"]
    assert manager.snapshot()["held"][0]["owner"] == "first"

    allow_first_exit.set()
    await asyncio.gather(first_task, second_task)

    assert entered == ["first", "first_exit", "second"]
    assert manager.snapshot() == {"held": []}


@pytest.mark.asyncio
async def test_engine_tool_call_batch_serializes_conflicting_file_resources():
    engine = object.__new__(AgentEngine)
    engine._resource_locks = ResourceLockManager()
    engine._parallel_tool_limit = 4
    active = 0
    max_active = 0

    async def fake_handle(
        self,
        tool_name,
        tool_input,
        tool_id,
        pending_tool_results,
        *,
        synthetic_tool_call=False,
        log_gateway_start=False,
        runtime_tool_call=None,
    ):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        pending_tool_results.append({
            "tool_use_id": tool_id,
            "tool_name": tool_name,
            "result": "ok",
        })
        active -= 1

    engine._handle_tool_call_event = types.MethodType(fake_handle, engine)
    calls = [
        RuntimeToolCall.from_provider_event({
            "id": "call_1",
            "name": "write_file",
            "input": {"path": "same.txt", "content": "a"},
        }),
        RuntimeToolCall.from_provider_event({
            "id": "call_2",
            "name": "write_file",
            "input": {"path": "same.txt", "content": "b"},
        }),
    ]
    pending: list[dict[str, str]] = []

    await engine._handle_tool_call_batch(calls, pending)

    assert max_active == 1
    assert [item["tool_use_id"] for item in pending] == ["call_1", "call_2"]


@pytest.mark.asyncio
async def test_engine_chat_entry_serializes_concurrent_runs():
    engine = object.__new__(AgentEngine)
    engine._chat_lock = None
    engine._chat_owner_task = None
    active = 0
    max_active = 0
    order: list[str] = []

    async def fake_chat_unlocked(self, user_input, emit_finished=True, turn_id=0):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        order.append(f"start:{user_input}")
        await asyncio.sleep(0.03)
        order.append(f"end:{user_input}")
        active -= 1
        return user_input

    engine._chat_unlocked = types.MethodType(fake_chat_unlocked, engine)

    result = await asyncio.gather(engine.chat("a"), engine.chat("b"))

    assert result == ["a", "b"]
    assert max_active == 1
    assert order == ["start:a", "end:a", "start:b", "end:b"]

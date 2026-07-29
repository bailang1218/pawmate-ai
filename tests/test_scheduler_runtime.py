from __future__ import annotations

import pytest

from pawmate.core.runtime.engine import AgentEngine


@pytest.mark.asyncio
async def test_busy_engine_queues_scheduled_task_instead_of_dropping(monkeypatch) -> None:
    engine = object.__new__(AgentEngine)
    engine._state = type("State", (), {"is_running": True})()
    calls: list[tuple[str, bool]] = []

    async def fake_chat(message: str, *, emit_finished: bool = False):
        calls.append((message, emit_finished))

    engine.chat = fake_chat
    monkeypatch.setattr("pawmate.core.runtime.engine.event_bus.publish", lambda _event: None)

    await engine._on_scheduled_fire("once_123", "send the report")

    assert calls == [
        (
            "[Scheduled task fired: once_123]\n"
            "Action to perform now: send the report\n\n"
            "Follow this instruction and briefly tell the user the result.",
            True,
        )
    ]

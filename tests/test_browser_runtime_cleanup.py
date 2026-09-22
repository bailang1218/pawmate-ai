from __future__ import annotations

import pytest

from pawmate.tools.browser import playwright_runtime as runtime
from pawmate.tools.browser.register import register_browser_tools
from pawmate.tools.core.registry import ToolRegistry


@pytest.mark.asyncio
async def test_browser_registration_adds_runtime_cleanup(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_close(mode: str = "current") -> str:
        calls.append(mode)
        return "closed"

    monkeypatch.setattr(runtime, "browser_close", fake_close)
    registry = ToolRegistry()
    register_browser_tools(registry)

    await registry.shutdown()

    assert calls == ["all"]


@pytest.mark.asyncio
async def test_browser_close_all_closes_every_registered_session(monkeypatch) -> None:
    first = type("Session", (), {"visibility": "foreground"})()
    second = type("Session", (), {"visibility": "background"})()
    closed: list[object] = []

    async def fake_close_session(session) -> bool:
        closed.append(session)
        return True

    class FakePlaywright:
        exited = False

        async def __aexit__(self, *_args):
            self.exited = True

    playwright = FakePlaywright()
    monkeypatch.setattr(runtime, "_browser_sessions", {"foreground": first, "background": second})
    monkeypatch.setattr(runtime, "_current_session", first)
    monkeypatch.setattr(runtime, "_playwright_ref", playwright)
    monkeypatch.setattr(runtime, "_browser_lock", None)
    monkeypatch.setattr(runtime, "_browser_lock_loop", None)
    monkeypatch.setattr(runtime, "_close_session", fake_close_session)

    await runtime.browser_close("all")

    assert closed == [first, second]
    assert runtime._browser_sessions == {}
    assert runtime._current_session is None
    assert runtime._browser_lock is None
    assert playwright.exited is True

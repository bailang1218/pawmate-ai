"""Contract tests for the browser deepfix patch.

These tests are intentionally mock-level: they validate the routing/session
contracts without launching a real browser. Run from the PawMate repo root after
copying this patch into place:

    python -m pytest tests/test_browser_session_contract.py -q
"""

from __future__ import annotations

import pytest

from pawmate.core.browser.router import BrowserAdapters, BrowserRouter


class FakeAdapters:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def attach_native(self) -> None:
        self.calls.append(("attach_native",))

    async def launch_managed(self, visibility: str, goal: str, url: str) -> str:
        resolved = "background" if visibility == "auto" and ("搜索" in goal or "search" in goal.lower()) else visibility
        if resolved in {"", "auto"}:
            resolved = "foreground"
        self.calls.append(("launch_managed", resolved, goal, url))
        return resolved

    async def navigate(self, url: str, visibility: str, goal: str, require_native: bool) -> None:
        self.calls.append(("navigate", visibility, require_native, url, goal))

    async def observe(self, visibility: str, goal: str, require_native: bool) -> dict:
        self.calls.append(("observe", visibility, require_native, goal))
        return {
            "url": "https://example.test/current",
            "title": "Example",
            "items": [{"selector": "#go", "text": "Go", "href": "", "disabled": False}],
        }

    async def click(self, ref: str) -> dict:
        self.calls.append(("click", ref))
        return {"ok": True}

    async def type_text(self, ref: str, value: str) -> dict:
        self.calls.append(("type_text", ref, value))
        return {"ok": True}

    async def fill(self, ref: str, value: str) -> dict:
        self.calls.append(("fill", ref, value))
        return {"ok": True}

    async def extract(self, query: str, goal: str, visibility: str, require_native: bool) -> dict:
        self.calls.append(("extract", visibility, require_native, query, goal))
        return {"ok": True, "text": "ok"}

    async def act(self, payload: dict) -> dict:
        self.calls.append(("act", payload.get("_visibility"), payload.get("_require_native_session"), payload.get("action")))
        return {"ok": True, "session": {"slot": "native" if payload.get("_require_native_session") else payload.get("_visibility")}}

    def as_dataclass(self) -> BrowserAdapters:
        return BrowserAdapters(
            attach_native=self.attach_native,
            launch_managed=self.launch_managed,
            navigate=self.navigate,
            observe=self.observe,
            click=self.click,
            type_text=self.type_text,
            fill=self.fill,
            extract=self.extract,
            act=self.act,
        )


@pytest.mark.asyncio
async def test_native_goto_threads_require_native_through_navigate_read_act_extract() -> None:
    fake = FakeAdapters()
    router = BrowserRouter(fake.as_dataclass(), current_goal="登录后台")

    await router.goto("https://admin.example.test", use_my_login=True, precheck=False)
    await router.read(visibility="auto")
    await router.act(ref="#go", action="click", visibility="auto")
    await router.extract("body", visibility="auto")

    assert ("attach_native",) in fake.calls
    assert ("navigate", "foreground", True, "https://admin.example.test", "登录后台") in fake.calls
    assert ("observe", "foreground", True, "登录后台") in fake.calls
    assert ("act", "foreground", True, "click") in fake.calls
    assert ("extract", "foreground", True, "body", "登录后台") in fake.calls


@pytest.mark.asyncio
async def test_background_goto_then_read_auto_stays_background() -> None:
    fake = FakeAdapters()
    router = BrowserRouter(fake.as_dataclass(), current_goal="搜索 AI agent")

    await router.goto("https://search.example.test", use_my_login=False, visibility="auto", precheck=False)
    await router.read(visibility="auto")
    await router.extract("summary", visibility="auto")

    assert ("launch_managed", "background", "搜索 AI agent", "https://search.example.test") in fake.calls
    assert ("navigate", "background", False, "https://search.example.test", "搜索 AI agent") in fake.calls
    assert ("observe", "background", False, "搜索 AI agent") in fake.calls
    assert ("extract", "background", False, "summary", "搜索 AI agent") in fake.calls


@pytest.mark.asyncio
async def test_empty_goal_does_not_clear_last_session_route() -> None:
    fake = FakeAdapters()
    router = BrowserRouter(fake.as_dataclass(), current_goal="搜索 AI agent")
    await router.goto("https://search.example.test", visibility="auto", precheck=False)

    # Simulate get_router(goal="") clearing only prompt goal, not route state.
    router.set_goal("")
    await router.read(visibility="auto")

    assert ("observe", "background", False, "") in fake.calls


@pytest.mark.asyncio
async def test_playwright_get_active_page_native_fail_closed(monkeypatch) -> None:
    from pawmate.tools.browser import playwright_runtime as pw

    pw._browser_sessions.clear()
    pw._current_session = None

    async def forbidden_ensure_session(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("require_native=True must not fall back to _ensure_session")

    monkeypatch.setattr(pw, "_ensure_session", forbidden_ensure_session)
    with pytest.raises(pw.BrowserDependencyError):
        await pw._get_active_page(require_native=True)


@pytest.mark.asyncio
async def test_browser_fill_form_empty_url_does_not_goto(monkeypatch) -> None:
    from pawmate.tools.browser import playwright_runtime as pw

    class FakePage:
        url = "https://example.test/form"

        async def goto(self, *args, **kwargs):  # pragma: no cover - should not be called
            raise AssertionError("browser_fill_form(url='') must not call page.goto('')")

        async def wait_for_selector(self, selector, timeout=0):
            return None

        async def click(self, selector):
            return None

        async def fill(self, selector, value):
            return None

        async def type(self, selector, value, delay=0):
            return None

        async def title(self):
            return "Form"

        async def evaluate(self, *args, **kwargs):
            return "filled"

    async def fake_get_active_page(*args, **kwargs):
        return object(), FakePage()

    monkeypatch.setattr(pw, "_get_active_page", fake_get_active_page)
    result = await pw.browser_fill_form(url="", fields={"#kw": "test"})
    assert "表单填写完成" in result

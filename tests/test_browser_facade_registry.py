import asyncio

from pawmate.tools.browser.facade import build_vision_subagent, register_browser_facade_tools
from pawmate.tools.core.registry import APPROVAL_CONFIRM, APPROVAL_NOTIFY, ToolRegistry
from pawmate.tools.browser import native as native_browser, playwright_runtime as playwright_browser


def test_browser_facade_registry_exposes_only_four_browser_tools():
    registry = ToolRegistry()
    register_browser_facade_tools(registry)
    names = {tool["name"] for tool in registry.list_tools()}
    assert names == {"browser_goto", "browser_read", "browser_act", "browser_extract"}
    assert registry.get_approval("browser_goto") == APPROVAL_NOTIFY
    assert registry.get_approval("browser_act") == APPROVAL_CONFIRM
    assert registry.get_approval("browser_read") == APPROVAL_NOTIFY
    assert registry.get_approval("browser_extract") == APPROVAL_NOTIFY


def test_low_level_browser_functions_remain_importable():
    assert callable(playwright_browser.browser_click)
    assert callable(playwright_browser.browser_observe)
    assert callable(native_browser.native_browser_click)
    assert callable(native_browser.native_browser_screenshot)


def test_browser_act_schema_exposes_legacy_and_runtime_actions():
    registry = ToolRegistry()
    register_browser_facade_tools(registry)
    schema = registry.get_tool("browser_act").input_schema
    actions = set(schema["properties"]["action"]["enum"])

    assert {"click", "type", "fill"}.issubset(actions)
    assert {"key", "hotkey", "scroll", "click_xy", "insert_text", "smart_type"}.issubset(actions)


def test_visual_page_screenshot_clicks_in_same_playwright_viewport(monkeypatch):
    clicks = []

    class FakeMouse:
        async def click(self, x, y):
            clicks.append((x, y))

    class FakePage:
        viewport_size = {"width": 800, "height": 600}
        mouse = FakeMouse()

        async def screenshot(self, **_kwargs):
            return None

        def is_closed(self):
            return False

    class FakeSession:
        page = FakePage()

    monkeypatch.setattr(playwright_browser, "_current_session", FakeSession())
    monkeypatch.setattr(playwright_browser, "_session_is_usable", lambda _session: True)
    monkeypatch.setattr("pawmate.tools.browser.facade._image_data_url", lambda _path: "data:image/png;base64,xx")

    agent = build_vision_subagent()
    _data_url, size = asyncio.run(agent._screenshot())
    asyncio.run(agent._click(123, 234))

    assert size == (800, 600)
    assert clicks == [(123, 234)]


def test_visual_native_screenshot_reuses_exact_window(monkeypatch):
    clicked = []

    async def fake_screenshot():
        return {
            "path": "unused.png",
            "window": {"id": 456, "rect": {"width": 1000, "height": 700}},
        }

    async def fake_click(**kwargs):
        clicked.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(playwright_browser, "_current_session", None)
    monkeypatch.setattr(playwright_browser, "_session_is_usable", lambda _session: False)
    monkeypatch.setattr(native_browser, "native_browser_screenshot", fake_screenshot)
    monkeypatch.setattr(native_browser, "native_browser_click", fake_click)
    monkeypatch.setattr("pawmate.tools.browser.facade._image_data_url", lambda _path: "data:image/png;base64,xx")

    agent = build_vision_subagent()
    _data_url, size = asyncio.run(agent._screenshot())
    asyncio.run(agent._click(50, 60))

    assert size == (1000, 700)
    assert clicked == [{"x": 50, "y": 60, "window_id": 456, "relative": True}]

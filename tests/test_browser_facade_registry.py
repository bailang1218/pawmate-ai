from pawmate.tools.browser_facade import register_browser_facade_tools
from pawmate.tools.registry import APPROVAL_CONFIRM, APPROVAL_NOTIFY, ToolRegistry
from pawmate.tools import native_browser, playwright_browser


def test_browser_facade_registry_exposes_only_four_browser_tools():
    registry = ToolRegistry()
    register_browser_facade_tools(registry)
    names = {tool["name"] for tool in registry.list_tools()}
    assert names == {"browser_goto", "browser_read", "browser_act", "browser_extract"}
    assert registry.get_approval("browser_goto") == APPROVAL_CONFIRM
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

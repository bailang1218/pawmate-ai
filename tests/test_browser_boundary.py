import asyncio
from types import SimpleNamespace

from pawmate.core.browser.boundary import BrowserActionType, BrowserRisk, classify_browser_action
from pawmate.tools.browser import facade as browser_facade
from pawmate.tools.core.registry import RiskLevel, SideEffectLevel, ToolRegistry


def test_browser_action_taxonomy_marks_coordinate_click_high_risk():
    action, risk = classify_browser_action("click_xy", x=100, y=200)

    assert action == BrowserActionType.COORDINATE_CLICK
    assert risk == BrowserRisk.HIGH


def test_browser_act_runtime_policy_skips_confirmation_only_for_low_risk_scroll():
    from pawmate.core.tools.tool_policy import evaluate_tool_policy
    from pawmate.tools.browser import facade as browser_facade
    from pawmate.tools.core.registry import ToolRegistry

    registry = ToolRegistry()
    browser_facade.register_browser_facade_tools(registry)
    tool = registry.get_tool("browser_act")

    scroll = evaluate_tool_policy(tool, {"action": "scroll", "deltaY": 800})
    coordinate_click = evaluate_tool_policy(tool, {"action": "click_xy", "x": 10, "y": 20})

    assert scroll.allowed is True
    assert scroll.requires_confirm is False
    assert scroll.risk == "low"
    assert scroll.side_effect == "read_only"
    assert scroll.metadata["approval"] == "notify"
    assert coordinate_click.requires_confirm is True
    assert coordinate_click.metadata["approval"] == "confirm"
    assert coordinate_click.risk == "high"


def test_browser_action_taxonomy_marks_submit_and_payment_high_risk():
    action, risk = classify_browser_action("click", intent="submit this form")
    assert action == BrowserActionType.SUBMIT
    assert risk == BrowserRisk.HIGH

    action, risk = classify_browser_action("click", intent="pay now")
    assert action == BrowserActionType.SUBMIT
    assert risk == BrowserRisk.CRITICAL


def test_browser_act_runtime_policy_preserves_critical_payment_risk():
    from pawmate.core.tools.tool_policy import evaluate_tool_policy

    registry = ToolRegistry()
    browser_facade.register_browser_facade_tools(registry)

    decision = evaluate_tool_policy(
        registry.get_tool("browser_act"),
        {"action": "click", "intent": "pay now"},
    )

    assert decision.allowed is True
    assert decision.requires_confirm is True
    assert decision.risk == "critical"


def test_browser_action_taxonomy_treats_enter_as_submit_even_without_intent():
    action, risk = classify_browser_action("hotkey", key="Control+Enter")

    assert action == BrowserActionType.SUBMIT
    assert risk == BrowserRisk.HIGH


def test_browser_facade_tool_metadata_exposes_risk_to_policy_layer():
    registry = ToolRegistry()
    browser_facade.register_browser_facade_tools(registry)

    act_tool = registry.get_tool("browser_act")
    read_tool = registry.get_tool("browser_read")

    assert act_tool.risk == RiskLevel.HIGH.value
    assert act_tool.side_effect == SideEffectLevel.EXTERNAL_WRITE.value
    assert read_tool.risk == RiskLevel.LOW.value
    assert read_tool.side_effect == SideEffectLevel.READ_ONLY.value


def test_browser_read_result_is_marked_untrusted(monkeypatch):
    class FakeRouter:
        async def read(self, visibility: str = "auto"):
            return SimpleNamespace(
                ok=True,
                url="https://example.test",
                title="Example",
                page_kind="public",
                modality="dom",
                elements=[],
                note="",
            )

    monkeypatch.setattr(browser_facade, "get_router", lambda goal="": FakeRouter())

    result = asyncio.run(browser_facade.browser_read())

    boundary = result["browser_boundary"]
    assert boundary["action"] == "read"
    assert boundary["backend"] == "dom"
    assert boundary["untrusted_observation"] is True
    assert boundary["sensitive"] is False


def test_browser_act_coordinate_click_records_high_risk_trace(monkeypatch):
    class FakeRouter:
        async def act(self, **_kwargs):
            return {
                "ok": True,
                "operation": "browser_act",
                "url": "https://example.test",
                "backend": "vision",
                "fallback": "coordinate",
            }

    monkeypatch.setattr(browser_facade, "get_router", lambda goal="": FakeRouter())

    result = asyncio.run(browser_facade.browser_act(action="click_xy", x=10, y=20))

    boundary = result["browser_boundary"]
    assert boundary["action"] == "coordinate_click"
    assert boundary["risk"] == "high"
    assert boundary["backend"] == "vision"
    assert boundary["fallback"] == "coordinate"


def test_browser_goto_login_session_is_marked_sensitive(monkeypatch):
    class FakeRouter:
        async def goto(self, url: str, use_my_login: bool = False, visibility: str = "auto"):
            return SimpleNamespace(
                ok=True,
                url=url,
                surface="native",
                reachability="ok",
                note="",
            )

    monkeypatch.setattr(browser_facade, "get_router", lambda goal="": FakeRouter())
    monkeypatch.setattr(
        browser_facade.pw,
        "_browser_session_info",
        lambda: {"attached": True, "url": "https://example.test/account"},
    )

    result = asyncio.run(browser_facade.browser_goto("https://example.test/account", use_my_login=True))

    boundary = result["browser_boundary"]
    assert boundary["action"] == "navigate"
    assert boundary["sensitive"] is True
    assert boundary["session"]["attached"] is True

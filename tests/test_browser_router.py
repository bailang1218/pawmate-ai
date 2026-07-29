import asyncio

from pawmate.core.browser.escalation import CdpAttachFailed, LoginRequired
from pawmate.core.browser.router import BrowserAdapters, BrowserRouter, BrowserVisionUnavailable
from pawmate.core.browser.surface import Modality, Target
from pawmate.core.browser.vision_subagent import SubAgentResult


def make_adapters(**over):
    calls = {"navigate": [], "attach_native": 0, "launch_managed": 0, "click": []}

    async def attach_native():
        calls["attach_native"] += 1
        if over.get("attach_raises"):
            raise CdpAttachFailed("no cdp")

    async def launch_managed(visibility: str = "auto", goal: str = "", url: str = ""):
        calls["launch_managed"] += 1
        return "background" if visibility == "background" else "foreground"

    async def navigate(url, visibility: str = "foreground", goal: str = "", require_native: bool = False):
        calls["navigate"].append((url, visibility, require_native, goal))

    async def observe(visibility: str = "foreground", goal: str = "", require_native: bool = False):
        return over.get("observe", {"url": "u", "title": "t", "items": []})

    async def click(ref):
        calls["click"].append(ref)
        return {"ok": True, "operation": "browser_click", "ref": ref}

    async def type_text(ref, value):
        return {"ok": True}

    async def fill(ref, value):
        return {"ok": True}

    async def extract(query, goal: str = "", visibility: str = "foreground", require_native: bool = False):
        return {"ok": True, "text": "x"}

    adapters = BrowserAdapters(attach_native, launch_managed, navigate, observe, click, type_text, fill, extract)
    return adapters, calls


def test_goto_login_routes_native_and_attaches():
    adapters, calls = make_adapters(
        observe={"url": "u", "title": "t", "items": [{"text": "x", "href": "/v", "selector": "a"}]}
    )
    router = BrowserRouter(adapters)
    result = asyncio.run(router.goto("https://x", use_my_login=True))
    assert result.ok and router.surface.target == Target.NATIVE
    assert calls["attach_native"] == 1 and calls["launch_managed"] == 0
    assert calls["navigate"] == [("https://x", "foreground", True, "")]


def test_goto_no_login_routes_managed():
    adapters, calls = make_adapters(
        observe={"url": "u", "title": "t", "items": [{"text": "x", "selector": "a", "href": "/v"}]}
    )
    router = BrowserRouter(adapters)
    result = asyncio.run(router.goto("https://x", use_my_login=False))
    assert result.ok and router.surface.target == Target.MANAGED
    assert calls["launch_managed"] == 1 and calls["attach_native"] == 0
    assert calls["navigate"] == [("https://x", "foreground", False, "")]


def test_goto_login_attach_fail_raises_login_required_not_downgrade():
    adapters, calls = make_adapters(attach_raises=True)
    router = BrowserRouter(adapters)
    try:
        asyncio.run(router.goto("https://x", use_my_login=True))
        assert False, "should raise"
    except LoginRequired:
        assert calls["launch_managed"] == 0


def test_goto_precheck_paywalled_returns_unreachable():
    observed = {
        "url": "https://www.bilibili.com/cheese/play/ss1",
        "title": "课",
        "items": [{"text": "购买后观看", "selector": "div.cheese-x"}],
    }
    adapters, _ = make_adapters(observe=observed)
    router = BrowserRouter(adapters, current_goal="在评论区写概览")
    result = asyncio.run(router.goto("https://www.bilibili.com/cheese/play/ss1", use_my_login=False))
    assert not result.ok and result.reachability["page_kind"] == "paywalled_course"


def test_read_returns_refs_from_dom():
    observed = {
        "url": "u",
        "title": "t",
        "items": [
            {
                "selector": "a.card:nth-of-type(3)",
                "text": "Agent智能体",
                "href": "/video/BV1zz",
                "role": "",
                "tag": "a",
                "ariaLabel": "打开视频",
                "bbox": {"x": 10, "y": 20, "w": 100, "h": 30},
            },
        ],
    }
    adapters, _ = make_adapters(observe=observed)
    router = BrowserRouter(adapters)
    result = asyncio.run(router.read())
    assert result.modality == "dom"
    assert result.elements[0]["ref"] == "a.card:nth-of-type(3)"
    assert result.elements[0]["href"] == "/video/BV1zz"
    assert result.elements[0]["aria_label"] == "打开视频"
    assert result.elements[0]["bbox"]["x"] == 10


def test_read_dom_empty_without_subagent_raises():
    adapters, _ = make_adapters(observe={"url": "u", "title": "t", "items": []})
    router = BrowserRouter(adapters, vision_subagent=None)
    try:
        asyncio.run(router.read())
        assert False
    except BrowserVisionUnavailable:
        pass


def test_read_dom_empty_with_subagent_switches_to_vision():
    class FakeSub:
        async def run(self, *, target, action="click"):
            return SubAgentResult(True, "click@(1,2)", 1, "verified")

    adapters, _ = make_adapters(observe={"url": "u", "title": "t", "items": []})
    router = BrowserRouter(adapters, vision_subagent=FakeSub())
    result = asyncio.run(router.read())
    assert result.modality == "vision" and router.surface.modality == Modality.DOM


def test_missing_ref_recovers_unique_fresh_dom_match_before_vision():
    class FakeSub:
        async def run(self, *, target, action="click"):
            raise AssertionError("vision must not run when DOM recovery succeeds")

    observed = {"url": "u", "title": "t", "items": [{"selector": "#submit-new", "text": "Submit"}]}
    adapters, calls = make_adapters(observe=observed)
    router = BrowserRouter(adapters, vision_subagent=FakeSub())
    out = asyncio.run(router.act(intent="Submit", action="click"))
    assert out["ok"] and calls["click"] == ["#submit-new"]


def test_act_dom_clicks_by_ref():
    adapters, calls = make_adapters()
    router = BrowserRouter(adapters)
    asyncio.run(router.act(ref="a.card", action="click"))
    assert calls["click"] == ["a.card"]


def test_act_vision_routes_to_subagent():
    class FakeSub:
        async def run(self, *, target, action="click"):
            assert target == "登录按钮"
            return SubAgentResult(True, "click@(5,6)", 1, "verified")

    adapters, _ = make_adapters(observe={"url": "u", "title": "t", "items": []})
    router = BrowserRouter(adapters, vision_subagent=FakeSub())
    asyncio.run(router.read())
    out = asyncio.run(router.act(intent="登录按钮", action="click"))
    assert out["ok"] and out["modality"] == "vision"


def test_act_missing_dom_ref_escalates_to_vision():
    class FakeSub:
        async def run(self, *, target, action="click"):
            assert target == "播放按钮"
            return SubAgentResult(True, "click@(30,40)", 1, "verified")

    adapters, calls = make_adapters()
    router = BrowserRouter(adapters, vision_subagent=FakeSub())
    out = asyncio.run(router.act(intent="播放按钮", action="click"))

    assert out["ok"] and out["modality"] == "vision"
    assert out["fallback"] == "missing_dom_ref"
    assert calls["click"] == []


def test_failed_dom_click_with_intent_escalates_to_vision():
    class FakeSub:
        async def run(self, *, target, action="click"):
            assert target == "提交"
            return SubAgentResult(True, "click@(50,60)", 1, "verified")

    adapters, _ = make_adapters()

    async def failed_act(_payload):
        return {"ok": False, "error_type": "all_backends_failed", "message": "stale ref"}

    adapters.act = failed_act
    router = BrowserRouter(adapters, vision_subagent=FakeSub())
    out = asyncio.run(router.act(ref="#stale", intent="提交", action="click"))

    assert out["ok"] and out["fallback"] == "dom_action_failed"
    assert out["dom_error"]["error_type"] == "all_backends_failed"

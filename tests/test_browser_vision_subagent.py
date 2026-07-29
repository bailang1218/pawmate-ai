import asyncio

from pawmate.core.browser.vision_subagent import VisionSubAgent
from pawmate.ocr_vision.models import LocateResult


def ok_loc(x=10, y=20):
    return LocateResult(ok=True, point={"x": x, "y": y}, bbox=None, confidence=0.9, reason="ok")


def fail_loc(reason="unlocated"):
    return LocateResult.fail(reason)


def build(locate_script, *, verify=True, max_steps=4):
    state = {"shots": 0, "clicks": [], "locate_calls": 0}
    sequence = list(locate_script)

    async def screenshot():
        state["shots"] += 1
        return "data:image/png;base64,xx", (800, 600)

    async def locate_fn(data_url, target, image_size):
        assert image_size == (800, 600)
        index = state["locate_calls"]
        state["locate_calls"] += 1
        return sequence[index] if index < len(sequence) else fail_loc()

    async def click(x, y):
        state["clicks"].append((x, y))

    agent = VisionSubAgent(screenshot_fn=screenshot, locate_fn=locate_fn, click_fn=click, verify=verify, max_steps=max_steps)
    return agent, state


def test_locate_then_verify_gone_is_success():
    agent, state = build([ok_loc(30, 40), fail_loc()])
    result = asyncio.run(agent.run(target="登录"))
    assert result.ok and result.final_state == "verified"
    assert state["clicks"] == [(30, 40)]


def test_no_verify_returns_acted():
    agent, _state = build([ok_loc(1, 2)], verify=False)
    result = asyncio.run(agent.run(target="x"))
    assert result.ok and result.final_state == "acted"


def test_unlocated_until_budget_then_structured_fail():
    agent, state = build([fail_loc(), fail_loc(), fail_loc(), fail_loc()], max_steps=4)
    result = asyncio.run(agent.run(target="x"))
    assert not result.ok and result.final_state in ("unlocated", "budget_exhausted")
    assert state["clicks"] == []


def test_click_is_never_repeated_when_target_remains_visible():
    agent, _state = build([ok_loc()] * 100, max_steps=3)
    result = asyncio.run(agent.run(target="x"))
    assert result.steps == 1 and result.ok and result.final_state == "acted_unverified"
    assert _state["clicks"] == [(10, 20)]


def test_returns_only_structured_no_prose():
    agent, _state = build([ok_loc(), fail_loc()])
    result = asyncio.run(agent.run(target="x"))
    data = result.as_dict()
    assert set(data.keys()) == {"ok", "action_taken", "steps", "final_state", "reason"}
    assert all(not isinstance(value, str) or key in ("action_taken", "final_state", "reason") for key, value in data.items())

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawmate.core.tools.tool_router import route_tools_for_turn


ALL = {
    "browser_goto",
    "browser_read",
    "browser_act",
    "browser_extract",
    "native_browser_click",
    "native_web_search",
    "capture_screenshot",
    "run_shell_command",
}
BROWSER = {"browser_goto", "browser_read", "browser_act", "browser_extract"}


def test_browser_request_can_always_reach_registered_browser_facade():
    route = route_tools_for_turn("open YouTube and show homepage recommendations", ALL)

    assert route.kind == "browser_task"
    assert route.tool_names == ALL
    assert route.matched_tool_names == BROWSER


def test_all_tools_visible_does_not_change_browser_capability_match():
    route = route_tools_for_turn("use my logged-in browser", ALL)

    assert route.tool_names == ALL
    assert route.matched_tool_names == BROWSER
    assert "native_browser_click" not in route.matched_tool_names
    assert "run_shell_command" not in route.matched_tool_names


def test_vision_and_shell_intents_keep_diagnostic_classification():
    vision_route = route_tools_for_turn("take a screenshot", ALL)
    shell_route = route_tools_for_turn("run a shell command", ALL)

    assert vision_route.kind == "vision_task"
    assert vision_route.tool_names == ALL
    assert vision_route.matched_tool_names == {"capture_screenshot"}
    assert shell_route.kind == "shell_task"
    assert shell_route.tool_names == ALL
    assert shell_route.matched_tool_names == {"run_shell_command"}

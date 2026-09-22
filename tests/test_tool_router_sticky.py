import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawmate.core.tools.tool_router import route_tools_for_turn


ALL = {
    "read_text_file",
    "write_file",
    "run_shell_command",
    "run_tests",
    "browser_goto",
    "browser_read",
    "browser_act",
    "browser_extract",
    "native_web_search",
    "search_memory",
}
BROWSER = {"browser_goto", "browser_read", "browser_act", "browser_extract"}


def test_action_intent_exposes_full_registry_but_records_narrow_match():
    route = route_tools_for_turn("install java and run tests", ALL)

    assert route.kind == "shell_task"
    assert route.tool_names == ALL
    assert {"run_shell_command", "run_tests"} <= route.matched_tool_names


def test_chat_still_exposes_registry_without_requiring_action_route():
    route = route_tools_for_turn("hello", ALL)

    assert route.kind == "chat"
    assert route.tool_names == ALL
    assert route.inherited_tool_names == set()


def test_browser_intent_exposes_full_registry_and_matches_facade():
    route = route_tools_for_turn("open https://youtube.com", ALL)

    assert route.kind == "browser_task"
    assert route.tool_names == ALL
    assert route.matched_tool_names == BROWSER


def test_followup_records_sticky_tools_without_using_them_as_allowlist():
    sticky = {"browser_goto", "browser_read"}
    route = route_tools_for_turn("continue", ALL, sticky_tools=sticky)

    assert route.kind == "chat"
    assert route.tool_names == ALL
    assert route.inherited_tool_names == sticky

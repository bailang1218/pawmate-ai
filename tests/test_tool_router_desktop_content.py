import pytest

from pawmate.core.tools.tool_router import route_tools_for_turn


BROWSER = {"browser_goto", "browser_read", "browser_act", "browser_extract"}
ALL = BROWSER | {
    "native_web_search",
    "run_powershell_query",
    "run_shell_command",
    "move_paths",
    "read_text_file",
    "search_file",
    "search_memory",
    "growth_status",
}


def test_desktop_content_classification_keeps_full_catalog_visible():
    route = route_tools_for_turn("desktop contents files", ALL)

    assert route.kind == "desktop_content_task"
    assert route.tool_names == ALL
    assert route.matched_tool_names == {"run_powershell_query"}


def test_file_organize_classification_does_not_inherit_previous_match():
    route = route_tools_for_turn(
        "organize and move files",
        ALL,
        sticky_tools={"search_memory", "browser_goto"},
    )

    assert route.kind == "file_organize_task"
    assert route.tool_names == ALL
    assert {"move_paths", "run_shell_command"} <= route.matched_tool_names
    assert route.inherited_tool_names == set()


def test_live_web_query_matches_search_while_browser_remains_visible():
    route = route_tools_for_turn("search latest weather news", ALL)

    assert route.kind == "web_search_task"
    assert route.tool_names == ALL
    assert route.matched_tool_names == {"native_web_search"}


@pytest.mark.parametrize(
    "text",
    [
        "test the login page at https://example.com",
        "read this webpage https://example.com",
        "open YouTube homepage",
    ],
)
def test_browser_intent_matches_facade_without_narrowing_registry(text):
    route = route_tools_for_turn(text, ALL)

    assert route.kind == "browser_task"
    assert route.tool_names == ALL
    assert route.matched_tool_names == BROWSER


def test_plain_chat_and_followup_both_receive_full_catalog():
    chat = route_tools_for_turn("hello", ALL)
    followup = route_tools_for_turn("continue", ALL, sticky_tools=BROWSER)

    assert chat.kind == "chat"
    assert chat.tool_names == ALL
    assert chat.inherited_tool_names == set()
    assert followup.kind == "chat"
    assert followup.tool_names == ALL
    assert followup.inherited_tool_names == BROWSER

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawmate.core.runtime.engine import AgentEngine, EngineState
from pawmate.core.tools.tool_router import route_tools_for_turn


ALL = {
    "browser_goto",
    "browser_read",
    "browser_act",
    "browser_extract",
    "native_web_search",
    "run_shell_command",
    "read_text_file",
}
BROWSER = {"browser_goto", "browser_read", "browser_act", "browser_extract"}


class Registry:
    def list_tools(self, names=None):
        wanted = set(names) if names is not None else set(ALL)
        return [
            {"name": name, "description": "", "input_schema": {}}
            for name in ALL
            if name in wanted
        ]


def make_engine():
    engine = AgentEngine.__new__(AgentEngine)
    engine._registry = Registry()
    engine._state = EngineState(tool_call_history=[])
    engine._recent_task_route_tool_names = set()
    engine._nested_task_route_tool_names = set()
    return engine


def test_browser_cluster_is_matched_while_full_registry_is_visible():
    route = route_tools_for_turn("browse YouTube", ALL)

    assert route.kind == "browser_task"
    assert route.tool_names == ALL
    assert route.matched_tool_names == BROWSER


def test_engine_can_remember_diagnostic_match_without_narrowing_catalog():
    engine = make_engine()
    browser_route = route_tools_for_turn("browse YouTube", ALL)
    engine._remember_recent_task_route(browser_route.matched_tool_names)

    followup = route_tools_for_turn(
        "continue",
        ALL,
        sticky_tools=engine._recent_task_route_tool_names,
    )

    assert followup.tool_names == ALL
    assert followup.inherited_tool_names == BROWSER


def test_new_intent_changes_match_not_registry_visibility():
    route = route_tools_for_turn("read file", ALL, sticky_tools=BROWSER)

    assert route.kind == "code_read_task"
    assert route.tool_names == ALL
    assert route.matched_tool_names == {"read_text_file"}
    assert route.inherited_tool_names == set()

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawmate.core.engine import AgentEngine, EngineState
from pawmate.core.tool_router import route_tools_for_turn


ALL = [
    "browser_goto",
    "browser_read",
    "browser_act",
    "browser_extract",
    "open_url",
    "download_file",
    "download_with_metadata",
    "run_shell_command",
    "read_text_file",
]


class Registry:
    def list_tools(self, names=None):
        wanted = set(names) if names is not None else set(ALL)
        return [{"name": name, "description": "", "input_schema": {}} for name in ALL if name in wanted]


def make_engine():
    engine = AgentEngine.__new__(AgentEngine)
    engine._registry = Registry()
    engine._state = EngineState(tool_call_history=[])
    engine._recent_task_route_tool_names = set()
    engine._nested_task_route_tool_names = set()
    return engine


def ck(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + info if info else ""))
    if not cond:
        raise AssertionError(name)


engine = make_engine()
browser_route = route_tools_for_turn("browser douyin task", ALL)
ck("browser route exposes goto", "browser_goto" in browser_route.tool_names, str(sorted(browser_route.tool_names)))
ck("browser route exposes read", "browser_read" in browser_route.tool_names, str(sorted(browser_route.tool_names)))
ck("browser route exposes act", "browser_act" in browser_route.tool_names, str(sorted(browser_route.tool_names)))
ck("browser route exposes extract", "browser_extract" in browser_route.tool_names, str(sorted(browser_route.tool_names)))
ck("browser route exposes full toolset", browser_route.kind == "all" and browser_route.matched_tool_names == set(ALL))

engine._remember_recent_task_route(browser_route.tool_names)
followup_route = route_tools_for_turn("continue", ALL, sticky_tools=engine._recent_task_route_tool_names)
ck("short follow-up still exposes full route", followup_route.kind == "all" and set(followup_route.tool_names) == set(ALL))
ck("short follow-up marks inherited", "browser_act" in followup_route.inherited_tool_names)

mixed_route = route_tools_for_turn("read file", ALL, sticky_tools=engine._recent_task_route_tool_names)
ck("new intent records full matched tools", mixed_route.matched_tool_names == set(ALL))
ck("new intent keeps inherited browser route", "browser_read" in mixed_route.inherited_tool_names)

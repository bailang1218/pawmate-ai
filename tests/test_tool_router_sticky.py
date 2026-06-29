import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawmate.core.tool_router import route_tools_for_turn as R


ALL = [
    "read_text_file",
    "write_file",
    "run_shell_command",
    "list_directory",
    "launch_desktop_app",
    "browser_goto",
    "browser_read",
    "browser_act",
    "browser_extract",
    "open_url",
    "download_file",
    "download_with_metadata",
    "search_memory",
    "core_remember",
    "schedule_once",
    "vision_analyze",
    "capture_screenshot",
    "ocr_image",
]


def run_checks(verbose=False):
    failures = []

    def ck(name, cond, info=""):
        if verbose:
            print(("PASS " if cond else "FAIL ") + name + ("  " + info if info else ""))
        if not cond:
            failures.append(name)

    r = R("帮我从0装一个java环境", ALL)
    ck("装java环境→非空(可执行)", len(r.tool_names) > 0 and "run_shell_command" in r.tool_names, f"kind={r.kind} n={len(r.tool_names)}")
    r = R("帮我部署一下这个服务", ALL)
    ck("部署→全量", r.kind == "all" and r.tool_names == set(ALL), f"kind={r.kind}")
    r = R("编译并运行测试", ALL)
    ck("编译→全量", r.kind == "all" and r.tool_names == set(ALL))
    r = R("继续", ALL)
    ck("继续(无sticky)→全量", r.kind == "all" and r.tool_names == set(ALL), f"kind={r.kind}")
    r = R("继续", ALL, sticky_tools={"browser_goto", "browser_act", "browser_read"})
    ck("继续+sticky→全量且记录继承", r.tool_names == set(ALL) and r.inherited_tool_names == {"browser_goto", "browser_act", "browser_read"} and r.kind == "all")
    r = R("你好呀", ALL)
    ck("纯闲聊→全量", r.kind == "all" and r.tool_names == set(ALL))
    r = R("总结一下刚才的结果", ALL)
    ck("总结→全量", r.kind == "all" and r.tool_names == set(ALL))
    r = R("你打开抖音给第三个视频评论", ALL)
    ck("抖音任务→含browser", any(t.startswith("browser_") for t in r.tool_names), f"n={len(r.tool_names)}")
    r = R("读取这个文件", ALL, sticky_tools={"browser_goto"})
    ck("匹配+sticky→全量且记录继承", r.tool_names == set(ALL) and r.inherited_tool_names == {"browser_goto"})
    r = R("在我的浏览器里操作", ALL)
    ck("我的浏览器→全量暴露", r.tool_names == set(ALL) and any(t.startswith("browser_") for t in r.tool_names))
    return failures


def test_tool_router_sticky_behavior():
    failures = run_checks()
    assert not failures, failures


if __name__ == "__main__":
    fails = run_checks(verbose=True)
    print("\n=== ", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
    sys.exit(1 if fails else 0)

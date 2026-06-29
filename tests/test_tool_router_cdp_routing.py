import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawmate.core.tool_router import route_tools_for_turn as R


ALL = [
    "browser_goto",
    "browser_read",
    "browser_act",
    "browser_extract",
    "open_url",
    "download_file",
    "vision_analyze",
    "capture_screenshot",
    "read_text_file",
]


def names(msg):
    return R(msg, ALL).tool_names


def has_browser(s):
    return any(t.startswith("browser_") for t in s)


def full(s):
    return s == set(ALL)


def run_checks(verbose=False):
    failures = []

    def ck(name, cond, info=""):
        if verbose:
            print(("PASS " if cond else "FAIL ") + name + ("  " + info if info else ""))
        if not cond:
            failures.append(name)

    s = names("你打开抖音给第三个视频评论")
    ck("普通浏览器任务→全量暴露", full(s) and has_browser(s), f"{sorted(s)}")
    s = names("用我的原生浏览器登录态操作一下")
    ck('"原生浏览器/登录态"→全量暴露', full(s) and has_browser(s), f"{sorted(s)}")
    s = names("在我已经打开的 Edge 里操作")
    ck('"已打开的Edge"→全量暴露', full(s) and has_browser(s), f"{sorted(s)}")
    s = names("用 native_browser 盲操一下这个窗口")
    ck('显式"盲操/native_browser"→全量但LLM不暴露native', full(s) and not any(t.startswith("native_browser_") for t in s), f"{sorted(s)}")
    s = names("给我截个图看看屏幕")
    ck("视觉截图→全量暴露", full(s), f"{sorted(s)}")
    s = names("帮我从0装一个java环境")
    ck("装java→全量暴露", full(s))
    s = names("你好呀")
    ck("闲聊→全量暴露", full(s))
    return failures


def test_tool_router_cdp_routing():
    failures = run_checks()
    assert not failures, failures


if __name__ == "__main__":
    fails = run_checks(verbose=True)
    print("\n=== ", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
    sys.exit(1 if fails else 0)

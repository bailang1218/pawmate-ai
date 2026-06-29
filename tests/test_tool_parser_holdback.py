import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawmate.core import tool_parser as tp


class Reg:
    def __init__(self, names):
        self.n = set(names)

    def has_tool(self, x):
        return x in self.n


reg = Reg(["read_text_file", "run_shell_command", "browser_goto"])


def vis(t):
    return tp.visible_text_before_textual_tool_protocol(t, reg)


def visany(t):
    return tp.visible_text_before_any_textual_tool_protocol(t)


def trunc(t):
    return tp.is_truncated_tool_protocol(t, reg)


def run_checks(verbose=False):
    failures = []

    def ck(name, got, exp):
        ok = got == exp
        if verbose:
            print(("PASS " if ok else "FAIL ") + name + f"  got={got!r} exp={exp!r}")
        if not ok:
            failures.append(name)

    dsml = (
        "我来读文件<||DSML||tool_calls><||DSML||invoke name=\"read_text_file\">"
        "<||DSML||parameter name=\"path\">a.txt</||DSML||parameter>"
        "</||DSML||invoke></||DSML||tool_calls>"
    )

    ck("hold bare <", vis("好的<"), "好的")
    ck("hold < + space", vis("好的< "), "好的")
    ck("hold <|", vis("好的<|"), "好的")
    ck("hold < ||(空格)", vis("好的< |"), "好的")
    ck("release <div", vis("代码<div"), "代码<div")
    ck("release a<b", vis("a<b"), "a<b")
    ck("plain prose", vis("普通文本没有标记"), "普通文本没有标记")
    ck("complete dsml visible", vis(dsml), "我来读文件")

    ck("any hold <", visany("好<"), "好")
    ck("any release <x", visany("好<x"), "好<x")

    ck("trunc lone <", trunc("<"), True)
    ck("trunc lone <|", trunc("<|"), True)
    ck("trunc lone < whitespace", trunc("  <  "), True)
    ck("trunc mid dsml (started not closed)", trunc("我来<||DSML||tool_calls|| name=\"read"), True)
    ck("trunc strong trailing <|", trunc("正在调用 <|"), True)
    ck("no-trunc complete dsml", trunc(dsml), False)
    ck("no-trunc plain prose", trunc("这是完整回答。"), False)
    ck("no-trunc literal < in code", trunc("用 <div> 标签包裹内容即可。"), False)
    ck("no-trunc trailing bare < in long prose", trunc("总之结果是 a 小于 b，记作 a <"), False)
    ck("no-trunc empty", trunc(""), False)
    return failures


def test_tool_parser_holdback_and_truncation():
    failures = run_checks()
    assert not failures, failures


if __name__ == "__main__":
    fails = run_checks(verbose=True)
    print("\n=== ", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
    sys.exit(1 if fails else 0)

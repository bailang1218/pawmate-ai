from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class ToolRoute:
    kind: str
    tool_names: set[str]
    reason: str
    matched_tool_names: set[str] = field(default_factory=set)
    inherited_tool_names: set[str] = field(default_factory=set)


_BROWSER_HINT = re.compile(
    r"(browser|web|url|http|https|网页|网站|浏览器|搜索|搜搜|打开.*网站|打开.*网页|"
    r"抖音|淘宝|京东|小红书|微博|哔哩|bilibili|chatgpt|deepseek|下载|browser_)",
    re.IGNORECASE,
)
_NATIVE_BROWSER_HINT = re.compile(
    r"(原生浏览器|我的浏览器|当前浏览器|已有浏览器|已经打开.*浏览器|已打开.*浏览器|"
    r"登录态|cookie|Edge|Chrome|native_browser_)",
    re.IGNORECASE,
)
_BLIND_OP_HINT = re.compile(
    r"(盲操|盲点|win32|gdi|快捷键操作|不走自动化|无法用?浏览器自动化|native_browser_)",
    re.IGNORECASE,
)
_LOCAL_HINT = re.compile(
    r"(文件|目录|工程|项目|代码|测试|pytest|git|进程|本地|电脑|系统|开机|自启动|注册表|"
    r"PowerShell|terminal|命令|运行了哪些|run_shell_command)",
    re.IGNORECASE,
)
_VISION_HINT = re.compile(r"(截图|截屏|屏幕|图片|图像|视觉|ocr|看图|识图|screenshot)", re.IGNORECASE)
_MEMORY_HINT = re.compile(r"(记住|记忆|回忆|之前|上次|你说过|remember|memory|recall)", re.IGNORECASE)
_SCHEDULE_HINT = re.compile(r"(提醒|定时|每天|取消.*任务|schedule|remind)", re.IGNORECASE)
_ACTION_HINT = re.compile(
    r"(打开|读取|写|保存|改|修|更新|运行|执行|搜索|下载|截图|点击|输入|关闭|删除|移动|创建|检查|测试|安装|装|卸载|部署|搭建|构建|编译|克隆|启动|生成|配置|查)",
    re.IGNORECASE,
)
_LOCAL_HINT_EN = re.compile(
    r"\b(file|folder|directory|path|workspace|project|repo|read file|write file|list directory|command|shell|script)\b",
    re.IGNORECASE,
)
_ACTION_HINT_EN = re.compile(
    r"\b(open|read|write|save|run|execute|search|download|click|type|create|move|delete|check|test|install|uninstall|deploy|build|compile|clone|launch|setup|generate|configure)\b",
    re.IGNORECASE,
)


MEMORY_TOOLS = {"core_remember", "core_forget", "take_note", "search_memory", "growth_status"}
SCHEDULER_TOOLS = {"schedule_once", "schedule_daily", "list_scheduled_tasks", "cancel_task"}
LOCAL_TOOLS = {
    "run_shell_command",
    "run_command",
    "run_script",
    "run_tests",
    "list_directory",
    "list_dir",
    "list_desktop",
    "get_known_folder",
    "read_text_file",
    "read_file",
    "read_symbol",
    "search_file",
    "write_file",
    "create_directory",
    "move_path",
    "move_paths",
    "launch_desktop_app",
}
VISION_TOOLS = {"capture_screenshot", "ocr_image", "vision_analyze", "browser_screenshot"}


def route_tools_for_turn(
    user_input: str,
    available_tool_names: Iterable[str],
    *,
    sticky_tools: Iterable[str] | None = None,
) -> ToolRoute:
    """Expose the full toolset; let the model decide what to call.

    Keyword pre-filtering only produced false negatives: a request whose
    phrasing missed the whitelist (e.g. "去B站搜") was left with zero tools and
    the agent could not act. The model is fully capable of choosing tools from
    the conversation. Dangerous tools remain gated by ConfirmGate
    (approval=confirm) regardless of routing, so exposing everything is safe.

    ``sticky_tools`` is kept for interface compatibility; it is reported via
    inherited_tool_names so callers depending on it keep working.
    """
    available = set(available_tool_names)
    if not available:
        return ToolRoute(kind="chat", tool_names=set(), reason="no tools available")
    sticky = set(sticky_tools or ()) & available
    return ToolRoute(
        kind="all",
        tool_names=set(available),
        reason="full toolset exposed; model decides",
        matched_tool_names=set(available),
        inherited_tool_names=sticky,
    )

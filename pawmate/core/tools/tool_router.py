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


# The router selects capability buckets rather than individual ad hoc tools.
# ConfirmGate and tool-specific boundaries remain the execution safety layer.
_BROWSER_HINT = re.compile(
    r"(browser|url|https?://|www\.|网页|网站|浏览器|打开.*网站|打开.*网页|"
    r"抖音|淘宝|京东|小红书|微博|哔哩|bilibili|chatgpt|deepseek|登录态|cookie|"
    r"点击|按钮|填写|表单|页面)",
    re.IGNORECASE,
)
_WEB_SEARCH_HINT = re.compile(
    r"(上网查|联网查|搜索|搜一下|查新闻|查官网|查天气|查台风|查资料|"
    r"最新|实时|新闻|热搜|天气|台风|气象|预警|路径|地震|航班|汇率|股价|"
    r"web\s+search|search\s+web|latest|recent|news|weather|typhoon|earthquake|"
    r"stock|exchange\s+rate)",
    re.IGNORECASE,
)
_URL_HINT = re.compile(r"https?://|www\.", re.IGNORECASE)
_STABLE_BROWSER_HINT = re.compile(
    r"\b(?:browser|browse|youtube|homepage|home\s*page)\b|youtu\.be",
    re.IGNORECASE,
)
_LOCAL_CODE_CONTEXT_HINT = re.compile(
    r"(代码|源码|工程|项目|仓库|文件|目录|路径|模块|函数|类|"
    r"\b(?:code|source|codebase|repo|repository|project|file|folder|directory|path|"
    r"module|function|class|pytest|unittest)\b)",
    re.IGNORECASE,
)
_DOWNLOAD_HINT = re.compile(r"(下载|保存链接|download|fetch\s+url|save\s+url)", re.IGNORECASE)
_CODE_EDIT_HINT = re.compile(
    r"(修改|修复|改代码|写入|保存|重构|patch|迁移|实现|新增|删除|移动|创建|跑测试|pytest|test)",
    re.IGNORECASE,
)
_FILE_ORGANIZE_HINT = re.compile(
    r"(整理|归类|分类|收拾|归档|清理.*桌面|移动.*文件|搬到|放到|"
    r"organize|categorize|sort\s+files|move\s+files)",
    re.IGNORECASE,
)
_CODE_READ_HINT = re.compile(
    r"(代码|工程|项目|仓库|文件|目录|读取|查看|分析|review|bug|树|"
    r"查找.*(文件|代码|目录|项目)|搜索.*(文件|代码|目录|项目)|"
    r"file|folder|directory|repo|project|read|grep|rg)",
    re.IGNORECASE,
)
_SHELL_HINT = re.compile(
    r"(命令|终端|shell|powershell|bash|运行命令|执行命令|"
    r"开机|自启|启动项|启动程序|进程|服务|系统信息|注册表|"
    r"startup|autostart|login item|process|service|registry|"
    r"install|安装|部署|编译|构建|npm|pip)",
    re.IGNORECASE,
)
_SYSTEM_QUERY_HINT = re.compile(
    r"(开机|自启|启动项|启动程序|进程|服务|系统信息|注册表|电脑.*状态|本机.*状态|"
    r"startup|autostart|login item|process|service|registry|system\s+info)",
    re.IGNORECASE,
)
_DESKTOP_CONTENT_HINT = re.compile(
    r"(桌面上|桌面里|桌面文件|桌面内容|桌面.*(有什么|有哪些|东西|文件|快捷方式|内容)|"
    r"desktop.*(items|files|folders|contents|shortcuts))",
    re.IGNORECASE,
)
_DESKTOP_HINT = re.compile(r"(桌面应用|启动应用|打开软件|本地程序|launch|desktop app|exe)", re.IGNORECASE)
_VISION_HINT = re.compile(r"(截图|截屏|屏幕|图片|图像|视觉|ocr|看图|识图|screenshot|image)", re.IGNORECASE)
_MEMORY_HINT = re.compile(r"(记住|记忆|回忆|之前|上次|你说过|remember|memory|recall)", re.IGNORECASE)
_SCHEDULE_HINT = re.compile(r"(提醒|定时|每天|取消.*任务|schedule|remind|timer)", re.IGNORECASE)


BROWSER_TOOLS = {"browser_goto", "browser_read", "browser_act", "browser_extract"}
WEB_SEARCH_TOOLS = {"native_web_search"}
DOWNLOAD_TOOLS = {"download_file", "download_with_metadata"}
CODE_READ_TOOLS = {
    "list_directory",
    "list_dir",
    "list_desktop",
    "get_known_folder",
    "read_text_file",
    "read_file",
    "read_symbol",
    "search_file",
}
CODE_EDIT_TOOLS = CODE_READ_TOOLS | {
    "write_file",
    "create_directory",
    "move_path",
    "move_paths",
    "run_tests",
}
SHELL_QUERY_TOOLS = {"run_powershell_query"}
SHELL_TOOLS = {"run_shell_command", "run_command", "run_script", "run_tests"}
FILE_ORGANIZE_TOOLS = CODE_EDIT_TOOLS | SHELL_QUERY_TOOLS | SHELL_TOOLS
DESKTOP_TOOLS = {"launch_desktop_app"}
VISION_TOOLS = {"capture_screenshot", "ocr_image", "vision_analyze"}
MEMORY_TOOLS = {
    "remember_memory",
    "consider_memory",
    "forget_memory",
    "search_memory",
    "search_history",
    "open_history_context",
    "core_remember",
    "core_forget",
    "take_note",
    "growth_status",
}
SCHEDULER_TOOLS = {"schedule_once", "schedule_daily", "list_scheduled_tasks", "cancel_task"}

CHAT_HELPER_TOOLS = {"search_memory", "search_history", "open_history_context", "growth_status"}

_STICKY_FOLLOWUP = re.compile(
    r"^\s*(continue|next|继续|接着|下一步|然后呢|好|好的|可以|行|没问题|"
    r"开始吧|动手|整吧|整理吧|再看看|仔细看看|再仔细看看|重新看看)\s*[。.!！?？~～]*\s*$",
    re.IGNORECASE,
)
_REFERENTIAL_FOLLOWUP = re.compile(
    r"(?:再|重新|继续|接着|换个方式).{0,16}(?:试|打开|看看|查看|搜索|操作|执行|访问)|"
    r"(?:再试|重试).{0,16}(?:桌面版|移动版|网页版|这个|那个|它)|"
    r"(?:桌面版|移动版|网页版).{0,12}(?:再试|重试|打开|看看)",
    re.IGNORECASE,
)


def _available(names: Iterable[str]) -> set[str]:
    return {str(name).strip() for name in names if str(name or "").strip()}


def _select(available: set[str], *groups: set[str]) -> set[str]:
    selected: set[str] = set()
    for group in groups:
        selected |= group & available
    return selected


def _should_inherit_sticky(text: str) -> bool:
    compact = str(text or "").strip()
    if not compact:
        return False
    return bool(
        _STICKY_FOLLOWUP.search(compact)
        or (len(compact) <= 48 and _REFERENTIAL_FOLLOWUP.search(compact))
    )


def route_tools_for_turn(
    user_input: str,
    available_tool_names: Iterable[str],
    *,
    sticky_tools: Iterable[str] | None = None,
    allow_browser_process_launch: bool = False,
) -> ToolRoute:
    """Expose the complete model-visible registry and classify turn intent.

    ``kind`` and ``matched_tool_names`` remain useful for evidence requirements,
    tracing, and follow-up context. They are not an execution allowlist. Runtime
    policy, argument validation, and ConfirmGate remain the authorization layer.
    """

    text = str(user_input or "")
    available = _available(available_tool_names)
    if not available:
        return ToolRoute(kind="chat", tool_names=set(), reason="no tools available")

    sticky = set(sticky_tools or ()) & available
    routes: list[tuple[str, re.Pattern[str], tuple[set[str], ...], str]] = [
        ("download_task", _DOWNLOAD_HINT, (DOWNLOAD_TOOLS,), "download intent"),
        ("vision_task", _VISION_HINT, (VISION_TOOLS,), "vision/screenshot intent"),
        ("schedule_task", _SCHEDULE_HINT, (SCHEDULER_TOOLS,), "scheduler intent"),
        ("desktop_content_task", _DESKTOP_CONTENT_HINT, (SHELL_QUERY_TOOLS,), "desktop content query intent"),
        ("file_organize_task", _FILE_ORGANIZE_HINT, (FILE_ORGANIZE_TOOLS,), "file organize intent"),
        ("memory_task", _MEMORY_HINT, (MEMORY_TOOLS,), "memory intent"),
        ("desktop_task", _DESKTOP_HINT, (DESKTOP_TOOLS, VISION_TOOLS), "desktop app intent"),
        ("system_query_task", _SYSTEM_QUERY_HINT, (SHELL_QUERY_TOOLS,), "local system query intent"),
        ("shell_task", _SHELL_HINT, (SHELL_TOOLS, CODE_READ_TOOLS), "shell intent"),
    ]

    if _STABLE_BROWSER_HINT.search(text):
        routes.append(
            ("browser_task", _STABLE_BROWSER_HINT, (BROWSER_TOOLS,), "stable browser intent")
        )
    if _URL_HINT.search(text):
        routes.append(("browser_task", _BROWSER_HINT, (BROWSER_TOOLS,), "URL browser intent"))
    if _LOCAL_CODE_CONTEXT_HINT.search(text):
        routes.extend(
            [
                ("code_edit_task", _CODE_EDIT_HINT, (CODE_EDIT_TOOLS,), "local code/file edit intent"),
                ("code_read_task", _CODE_READ_HINT, (CODE_READ_TOOLS,), "local code/file read intent"),
            ]
        )
    routes.extend(
        [
            ("browser_task", _BROWSER_HINT, (BROWSER_TOOLS,), "browser intent"),
            ("web_search_task", _WEB_SEARCH_HINT, (WEB_SEARCH_TOOLS,), "provider-native web search intent"),
            ("code_edit_task", _CODE_EDIT_HINT, (CODE_EDIT_TOOLS,), "code/file edit intent"),
            ("code_read_task", _CODE_READ_HINT, (CODE_READ_TOOLS,), "code/file read intent"),
        ]
    )

    for kind, pattern, groups, reason in routes:
        if pattern.search(text):
            matched = _select(available, *groups)
            if kind == "browser_task" and allow_browser_process_launch:
                matched |= {"run_shell_command"} & available
            if matched:
                return ToolRoute(
                    kind=kind,
                    tool_names=set(available),
                    reason=f"{reason}; full model-visible registry exposed",
                    matched_tool_names=matched,
                    inherited_tool_names=set(),
                )

    inherited = sticky if _should_inherit_sticky(text) else set()
    helpers = _select(available, CHAT_HELPER_TOOLS) | inherited
    return ToolRoute(
        kind="chat",
        tool_names=set(available),
        reason=(
            "referential follow-up; full model-visible registry exposed"
            if inherited
            else "no action intent; full model-visible registry exposed"
        ),
        matched_tool_names=helpers - inherited,
        inherited_tool_names=inherited,
    )

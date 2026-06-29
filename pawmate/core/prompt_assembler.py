"""
PromptAssembler — 结构化 System Prompt 组装器

职责：
  1. 按功能区组装 prompt section
  2. 自动从 ToolRegistry 读取工具描述
  3. 从 pawmate/data/ 读取引导文件（SOUL.md, RULES.md 等）
  4. 注入运行时信息（时间、工作区路径、sandbox 模式）

用法：
    prompt = (PromptAssembler()
        .with_tools(tool_registry)
        .with_language_mode("zh-CN")
        .with_bootstrap_files(base_dir)
        .with_sandbox_mode("safe")
        .assemble())
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pawmate.tools.registry import ToolRegistry

_logger = logging.getLogger("pawmate")

# ── 默认引导文件列表 ──────────────────────────────────────────
_BOOTSTRAP_FILES = [
    ("SOUL.md", "=== SOUL.md ==="),
    ("IDENTITY.md", "=== IDENTITY.md ==="),
    ("USER.md", "=== USER.md ==="),
    ("MEMORY.md", "=== MEMORY.md ==="),
    ("RULES.md", "=== RULES.md ==="),
    ("HEARTBEAT.md", "=== HEARTBEAT.md ==="),
]

# ── 语言规则模板 ──────────────────────────────────────────────
_LANGUAGE_RULES = {
    "auto": (
        "\n## 语言\n"
        "请根据用户输入语言自动选择回复语言。"
        "若用户没有明确要求，优先使用中文。"
    ),
    "zh-CN": (
        "\n## 语言\n"
        "除非用户明确要求其他语言，否则请使用简体中文回复。"
    ),
    "en-US": (
        "\n## Language\n"
        "Unless the user explicitly asks otherwise, respond in English."
    ),
}

_VALID_LANGUAGE_MODES = tuple(_LANGUAGE_RULES.keys())
_VALID_LC = tuple(k.lower() for k in _VALID_LANGUAGE_MODES)

# ── Sandbox 模式描述 ─────────────────────────────────────────
_SANDBOX_DESCRIPTIONS = {
    "safe": "安全模式 -- 仅允许读取类操作（查看文件、搜索、浏览）",
    "dev": "开发模式 -- 允许读取和有限的写操作",
    "full": "完全模式 -- 允许大多数操作",
    "off": "无限制 -- 沙箱已禁用（不推荐）",
}

# ── Provider 显示名映射 ──────────────────────────────────────
_PROVIDER_NAMES = {
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI (GPT)",
    "deepseek": "DeepSeek",
    "qwen": "通义千问 (Qwen)",
    "gemini": "Google Gemini",
    "minimaxi": "MiniMax (ABAB)",
    "auto": "自动路由",
}


class PromptAssembler:
    """结构化 System Prompt 组装器（Builder 模式）"""

    def __init__(self) -> None:
        self._sections: list[str] = []

        # ── 可配置字段 ──
        self._tool_registry: Optional[ToolRegistry] = None
        self._language_mode: str = "auto"
        self._sandbox_mode: str = "safe"
        self._provider: str = ""
        self._model: str = ""
        self._workspace: str = ""
        self._bootstrap_dir: Optional[Path] = None
        self._extra_rules: list[str] = []
        self._rules_loaded: bool = False  # RULES.md 是否已加载

    # ══════════════════════════════════════════════════════════
    # Builder 链式方法
    # ══════════════════════════════════════════════════════════

    def with_tools(self, registry: ToolRegistry) -> PromptAssembler:
        """注入工具注册表以自动生成工具描述。"""
        self._tool_registry = registry
        return self

    def with_language_mode(self, mode: str) -> PromptAssembler:
        """设置语言模式：auto / zh-CN / en-US。"""
        raw = mode.strip()
        if raw not in _VALID_LANGUAGE_MODES:
            raw_lc = raw.lower()
            if raw_lc in _VALID_LC:
                # 用户传了 "en-us" -> 映射到 "en-US"
                idx = _VALID_LC.index(raw_lc)
                raw = _VALID_LANGUAGE_MODES[idx]
            else:
                raw = "auto"
        self._language_mode = raw
        return self

    def with_sandbox_mode(self, mode: str) -> PromptAssembler:
        """设置沙箱模式：safe / dev / full / off。"""
        self._sandbox_mode = mode.strip().lower() if mode else "safe"
        return self

    def with_provider(self, provider: str, model: str) -> PromptAssembler:
        """设置 LLM 提供商和模型名。"""
        self._provider = _PROVIDER_NAMES.get(provider.strip().lower(), provider.strip())
        self._model = model.strip()
        return self

    def with_workspace(self, path: str) -> PromptAssembler:
        """设置工作区路径。"""
        self._workspace = path.strip()
        return self

    def with_bootstrap_files(self, data_dir: str | Path) -> PromptAssembler:
        """设置引导文件目录（通常是 pawmate/data/）。"""
        self._bootstrap_dir = Path(data_dir) if isinstance(data_dir, str) else data_dir
        return self

    def with_extra_rule(self, rule: str) -> PromptAssembler:
        """添加额外行为规则。"""
        self._extra_rules.append(rule.strip())
        return self

    # ══════════════════════════════════════════════════════════
    # 组装
    # ══════════════════════════════════════════════════════════

    def assemble(self) -> str:
        """按固定顺序组装所有 section，返回完整 system prompt。"""
        self._sections.clear()

        self._add_identity_section()
        self._add_bootstrap_sections()
        self._add_tools_section()
        self._add_workflow_section()
        self._add_rules_section()
        self._add_language_section()
        return "\n\n".join(self._sections)

    # ── Section: 身份 ──────────────────────────────────────

    def _add_identity_section(self) -> None:
        lines = [
            "## 身份",
            "你是 PawMate，一个运行在用户本地桌面端的 AI 助手。",
            "你可以帮助用户完成聊天、文件阅读、网页自动化、工具调用、配置管理和工作流辅助。",
            "你应该用自然、清晰、可靠的方式回答问题。",
        ]
        self._sections.append("\n".join(lines))

    # ── Section: 引导文件 ──────────────────────────────────

    def _add_bootstrap_sections(self) -> None:
        if not self._bootstrap_dir or not self._bootstrap_dir.is_dir():
            return

        for filename, header in _BOOTSTRAP_FILES:
            path = self._bootstrap_dir / filename
            if not path.exists():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace").strip()
                if content:
                    self._sections.append(f"{header}\n\n{content}")
                    if filename == "RULES.md":
                        self._rules_loaded = True
            except Exception as e:
                _logger.warning("[Prompt] 读取 %s 失败: %s", filename, e)

    # ── Section: 工具调用 ──────────────────────────────────

    def _add_tools_section(self) -> None:
        lines = [
            "## 工具调用",
            "你可以读取文件、运行命令、搜索网页、填写表单、点击按钮 -- 像真人一样操作电脑。",
            "",
            "### 可用工具",
        ]

        lines.extend([
            "Tool schemas are selected locally for each turn before the model call.",
            "Use only the tools exposed with the current request; ordinary chat may expose no tools.",
            "Prefer browser/native/file/memory tools according to the local route instead of guessing from a global tool list.",
        ])
        self._sections.append("\n".join(lines))
        return

        if self._tool_registry:
            tool_defs = self._tool_registry.list_tools()
            for t in sorted(tool_defs, key=lambda x: x.get("name", "")):
                name = t.get("name", "?")
                desc = t.get("description", "").strip()
                # 跳过 MCP 兼容别名，减少 prompt 噪音
                if desc.startswith("Alias of") or desc.startswith("Alias:"):
                    continue
                lines.append(f"- **{name}** -- {desc}")
        else:
            lines.append("（工具未加载）")

        self._sections.append("\n".join(lines))

    # ── Section: 常用工作流 ────────────────────────────────

    def _add_workflow_section(self) -> None:
        text = (
            "## 常用工作流\n\n"
            "遇到链接/网页任务时，优先用 browser_goto 而不是 open_url。\n"
            "浏览器自动化只通过 4 个 facade 动词暴露给模型：browser_goto、browser_read、browser_act、browser_extract。"
            "browser_act 支持 click/type/fill/key/hotkey/scroll/click_xy/insert_text/smart_type；"
            "底层 Playwright/CDP/native 函数只由 adapter 在进程内调用，不要直接调用旧 browser_observe/browser_click/native_browser_*。\n"
            "浏览器操作规则（必须严格遵守）：\n"
            "- 需要操作浏览器时，一律调用 browser_goto / browser_read / browser_act / browser_extract。\n"
            "- 禁止用 run_shell_command 手动启动浏览器，例如 msedge --remote-debugging-port=9222。\n"
            "- 禁止要求用户关闭他正在使用的 Edge/Chrome，禁止要求用户手动加调试端口重启浏览器。\n"
            "- 用户明确要求使用已有登录态时，browser_goto 传 use_my_login=true；挂载失败时停下说明，不要静默切 managed。\n"
            "- 若 browser_goto 报错，停下并把真实错误告诉用户，不要转去 run_shell_command 或坐标盲操。\n"
            "登录后台、客服系统、闲鱼/淘宝等需要登录态的任务，用 browser_goto(use_my_login=true)。\n\n"
            "**典型组合：**\n"
            "- **查资料：** browser_goto -> browser_read -> browser_extract\n"
            "- **登录/客服/后台：** browser_goto(use_my_login=true) -> browser_read -> browser_act\n"
            "- **页面点击：** browser_read 获取 ref -> browser_act；不要凭空猜 selector\n"
            "- **不可达页面：** browser_goto 的 reachability 会报告付费课/登录墙/404，收到后干净停止"
        )
        self._sections.append(text)

    # ── Section: 规则 + 安全约束 ───────────────────────────

    def _add_rules_section(self) -> None:
        """
        添加约束与安全 section。

        如果 RULES.md 已加载，则跳过硬编码规则（避免重复），
        只追加额外规则和沙箱状态。
        如果 RULES.md 不存在，则使用内置默认规则。
        """
        lines = [
            "## 约束与安全",
            "",
        ]

        if not self._rules_loaded:
            # RULES.md 不存在，使用内置默认规则
            lines += [
                "### 行为规则",
                "1. 不要说自己只是 Web UI 原型",
                "2. 不要说「还没有接入真实 AI」，除非系统确实处于 mock 模式",
                "3. 不暴露内部实现细节，除非用户主动询问",
                "4. 如果正在处理工具调用或文件读取，用简洁方式说明进度",
                "5. 如果用户只是普通聊天，直接自然回答",
                "6. 回答要务实、清楚，避免空泛",
                "",
                "### 安全约束",
                "- 禁止伪调用 -- 需要执行就发起真实工具调用",
                "- 下载文件用 download_file / download_with_metadata",
                "- 桌面应用用 launch_desktop_app",
                "- 不要主动创建 .bat / .ps1 / .cmd 安装脚本",
                "- 不要诱导用户双击未知脚本或可执行文件",
                "- 安装类操作只能通过 run_shell_command 在用户授权后执行",
            ]

        # 追加额外规则（无论是否加载 RULES.md 都追加）
        for rule in self._extra_rules:
            lines.append(f"- {rule}")

        # 沙箱模式提示
        sandbox_desc = _SANDBOX_DESCRIPTIONS.get(
            self._sandbox_mode, _SANDBOX_DESCRIPTIONS["safe"]
        )
        lines.append("")
        lines.append("### 沙箱状态")
        lines.append(f"当前 sandbox_mode = `{self._sandbox_mode}` -- {sandbox_desc}")

        self._sections.append("\n".join(lines))

    # ── Section: 语言 ─────────────────────────────────────

    def _add_language_section(self) -> None:
        rule = _LANGUAGE_RULES.get(self._language_mode, _LANGUAGE_RULES["auto"])
        self._sections.append(rule.strip())

    # ── Section: 运行时信息 ────────────────────────────────

    def _add_runtime_section(self) -> None:
        return None


# ══════════════════════════════════════════════════════════════
# 便捷工厂函数
# ══════════════════════════════════════════════════════════════

def build_prompt_from_config(
    config: dict,
    tool_registry: ToolRegistry,
    *,
    bootstrap_dir: str | Path | None = None,
) -> str:
    """从配置字典和工具注册表构建完整 system prompt。

    Args:
        config: 应用配置字典（来自 pawmate/config.json）
        tool_registry: 已注册好的工具注册表
        bootstrap_dir: 引导文件目录，默认 pawmate/data/

    Returns:
        组装好的 system prompt 字符串
    """
    import pawmate.config as _cfg

    if bootstrap_dir is None:
        bootstrap_dir = Path(__file__).parent.parent / "data"

    # 语言模式
    assistant_cfg = config.get("assistant", {})
    language_mode = str(assistant_cfg.get("language_mode", "auto")).strip()
    if language_mode not in _VALID_LANGUAGE_MODES:
        raw_lc = language_mode.lower()
        if raw_lc in _VALID_LC:
            idx = _VALID_LC.index(raw_lc)
            language_mode = _VALID_LANGUAGE_MODES[idx]
        else:
            language_mode = "auto"

    # 提供商 / 模型
    llm_cfg = config.get("llm", {})
    provider = str(llm_cfg.get("provider", "")).strip()
    model = ""
    llm_mode = str(llm_cfg.get("mode", "")).strip().lower()
    if llm_mode in {"auto", "router", "route"}:
        provider = "auto"
    if provider:
        model = str(llm_cfg.get(provider, {}).get("model", "")).strip()

    # 沙箱模式
    security_cfg = config.get("security", {})
    sandbox_mode = str(security_cfg.get("sandbox_mode", "safe")).strip().lower()

    assembler = (PromptAssembler()
        .with_tools(tool_registry)
        .with_language_mode(language_mode)
        .with_sandbox_mode(sandbox_mode)
        .with_provider(provider, model)
        .with_bootstrap_files(bootstrap_dir)
    )

    return assembler.assemble()


def build_runtime_prompt(
    tool_registry,
    core_mem=None,
    system_prompt: Optional[str] = None,
    app_config: Optional[dict] = None,
) -> str:
    """
    构建引擎运行时 system prompt。

    优先级: 显式传入 > app_config 自动构建 > DEFAULT_SYSTEM_PROMPT
    追加: 记忆使用规则 + 定时任务规则 + Core Memory 注入

    Args:
        tool_registry: ToolRegistry 实例
        core_mem: CoreMemory 实例（可选，用于注入）
        system_prompt: 显式 prompt 覆盖
        app_config: 应用配置（用于 PromptAssembler 构建）

    Returns:
        完整的 system prompt 字符串
    """
    from pawmate.core.llm_factory import DEFAULT_SYSTEM_PROMPT

    # 1. 基础 prompt 三选一
    if system_prompt is not None:
        final = system_prompt
    elif app_config is not None:
        final = build_prompt_from_config(app_config, tool_registry)
        _logger.info("[Prompt] 自动构建 system prompt (%d chars)", len(final))
    else:
        final = DEFAULT_SYSTEM_PROMPT

    # 2. 记忆使用规则
    final += (
        "\n## Memory usage rules\n"
        "You have two memory layers:\n"
        "- **Long-term memory**: Shown below in 'Long-term memory about the user'. "
        "Already in your context — no query needed. Capacity strictly limited.\n"
        "- **Short-term notes**: Facts/events from conversation. \n"
        "NOT automatically in your context. Use search_memory() to retrieve.\n\n"
        "When to use which:\n"
        "- User says 'remember this' or it's a permanent key fact → core_remember(key, value)\n"
        "- Long-term memory is full → core_forget(key) first, then write\n"
        "- Something mentioned that might be useful later → take_note(content), don't use core\n"
        "- User says 'remember when…', 'you said…', 'that thing about…' → search_memory(query)\n"
        "- Chitchat or temporary task details → don't record anything\n"
    )

    # 3. 定时任务规则
    final += (
        "\n## Scheduled task system\n"
        "You can schedule tasks to run automatically at a future time.\n"
        "Tools:\n"
        "- schedule_once(delay_seconds, action)\n"
        "- schedule_daily(hour, minute, action)\n"
        "- list_scheduled_tasks()\n"
        "- cancel_task(task_id)\n\n"
        "When to use:\n"
        "- User says 'in X minutes/hours do Y' → schedule_once (compute delay_seconds)\n"
        "- User says 'every day at X remind me' → schedule_daily\n"
        "- User asks 'what tasks do I have' → list_scheduled_tasks\n"
        "- User says 'cancel that task' → list then cancel_task\n\n"
        "Action format:\n"
        "- Second-person instruction to your future self\n"
        "- Example: 'open baidu and take a screenshot, tell the user it worked'\n"
        "- When the task fires, you will receive a [Scheduled task fired] message."
    )

    # 4. 注入 Core Memory
    if core_mem is not None:
        core_text = core_mem.format_for_prompt()
        if core_text:
            final += "\n\n" + core_text

    return final

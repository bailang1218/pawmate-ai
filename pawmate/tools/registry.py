"""
工具注册表（Registry）

统一管理所有工具定义和处理器
支持静态注册（内置技能）和动态注册（MCP server）

Approval 级别：
  - "auto":    直接执行，无需用户感知
  - "notify":  执行前通知 UI（显示卡片），不阻塞
  - "confirm": 执行前弹确认框，等待用户批准
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, Any, Optional
import inspect
from pawmate.core.redaction import redact_for_json, redact_text

# ── 可选审批级别 ──────────────────────────────────────────────
APPROVAL_AUTO: str = "auto"       # 静默执行
APPROVAL_NOTIFY: str = "notify"   # 通知但不阻塞
APPROVAL_CONFIRM: str = "confirm" # 弹确认框
VALID_APPROVALS = (APPROVAL_AUTO, APPROVAL_NOTIFY, APPROVAL_CONFIRM)


@dataclass(frozen=True)
class ApprovalDecision:
    """结构化审批结果"""
    allowed: bool = False
    reason: str = "unavailable"  # approved | denied | timeout | unavailable | pending

    def to_tool_result(self, tool_name: str) -> str:
        """将审批结果转为工具返回字符串"""
        mapping = {
            "denied": f"[approval_denied] 用户明确拒绝了工具调用: {tool_name}",
            "timeout": f"[approval_timeout] 等待用户确认超时，工具未执行: {tool_name}",
            "unavailable": f"[approval_unavailable] 当前没有可用的审批 UI，工具未执行: {tool_name}",
        }
        return mapping.get(self.reason, f"[denied] 用户拒绝了工具调用: {tool_name}")


@dataclass
class ToolDef:
    """工具定义"""
    name: str
    description: str
    input_schema: Dict[str, Any]  # JSON Schema
    handler: Callable  # async or sync callable
    approval: str = APPROVAL_AUTO  # 审批级别
    source: str = "builtin"  # 来源标识：builtin 或 mcp:<server_name>


@dataclass(frozen=True)
class ToolResult:
    """Normalized tool result inside ToolRegistry.

    ToolRegistry.execute() returns this structure. source_kind preserves the
    handler's original return shape so legacy projection can stay compatible
    for dict, str, and other values at the consumer boundary.
    """

    source_kind: str
    operation: str
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    ok: Optional[bool] = None
    error_type: Optional[str] = None
    message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, tool_name: str, raw: Any) -> "ToolResult":
        if isinstance(raw, dict):
            return cls(
                source_kind="dict",
                operation=str(raw.get("operation") or tool_name),
                data=raw,
                ok=raw.get("ok") if isinstance(raw.get("ok"), bool) else None,
                error_type=raw.get("error_type") if isinstance(raw.get("error_type"), str) else None,
                message=raw.get("message") if isinstance(raw.get("message"), str) else None,
                metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
            )
        if isinstance(raw, str):
            return cls(source_kind="str", operation=tool_name, text=raw)
        return cls(source_kind="other", operation=tool_name, text=str(raw))

    def to_legacy_string(self) -> str:
        if self.source_kind == "dict":
            return redact_for_json(self.data)
        return redact_text(self.text)


class ToolRegistry:
    """
    工具注册与调度中心

    支持：
      - register(tool_def)：单个工具注册
      - register_bulk(tools)：批量注册（供 MCP 加载）
      - execute(name, input)：执行工具
      - list_tools()：返回工具定义（供 LLM）
    """

    def __init__(self):
        """初始化注册表"""
        self._tools: Dict[str, ToolDef] = {}
        self._cleanup_hooks: list[Callable] = []
        # 审批回调：外部注册后，在需要 confirm 时被调用
        # 签名: async def on_confirm(name, input_data) -> bool
        self._confirm_callback: Optional[Callable] = None
        # 上一次审批决策结果（供 execute 读取具体 reason）
        self._last_approval: ApprovalDecision = ApprovalDecision(allowed=True, reason="approved")

    def set_confirm_callback(self, cb: Callable) -> None:
        """注册确认回调。

        Callable 签名:
            async def callback(tool_name: str, input_data: dict) -> bool
        返回 True=允许执行, False=拒绝。
        """
        self._confirm_callback = cb

    def register(self, tool_def: ToolDef) -> None:
        """
        注册单个工具

        安全策略：
        - builtin 工具优先，MCP 工具不能覆盖 builtin
        - MCP 同名工具会被拒绝并记录 warning
        - MCP 工具自动添加 mcp:<server_name> 命名空间

        Args:
            tool_def: 工具定义
        """
        _log = logging.getLogger("pawmate")
        existing = self._tools.get(tool_def.name)
        if existing is not None:
            # builtin 工具不能被覆盖
            if existing.source == "builtin" and tool_def.source.startswith("mcp:"):
                _log.warning(
                    "[ToolRegistry] collision rejected source=%s name=%s (existing=builtin)",
                    tool_def.source,
                    tool_def.name,
                )
                return
            _log.warning(
                "[ToolRegistry] overwriting source=%s name=%s (old=%s)",
                tool_def.source,
                tool_def.name,
                existing.source,
            )

        self._tools[tool_def.name] = tool_def
        _log.info(
            "[ToolRegistry] registered source=%s name=%s approval=%s",
            tool_def.source,
            tool_def.name,
            tool_def.approval,
        )

    def register_bulk(self, tools: Dict[str, ToolDef]) -> None:
        """
        批量注册工具

        Args:
            tools: {工具名 -> 工具定义} 字典
        """
        self._tools.update(tools)

    async def execute(self, name: str, input_data: Dict[str, Any]) -> ToolResult:
        """
        执行工具（含审批检查）

        流程：
          1. 检查工具是否存在
          2. 检查 approval 级别
             - auto: 直接执行
             - notify: 对 UI 发通知信号，不阻塞
             - confirm: 对 UI 发确认请求，等待用户响应
          3. 执行 handler

        Args:
            name: 工具名
            input_data: 输入参数字典

        Returns:
            工具执行结果

        Raises:
            KeyError: 工具不存在
        """
        _log = logging.getLogger("pawmate")

        if name not in self._tools:
            raise KeyError(f"Tool '{name}' not registered")

        tool_def = self._tools[name]

        # ── approval 检查 ───────────────────────────────────
        if tool_def.approval == APPROVAL_NOTIFY:
            try:
                from pawmate.bridge.contracts import ToolNotifyEvent
                from pawmate.bridge.event_bus import event_bus

                event_bus.publish(ToolNotifyEvent(name, redact_for_json(input_data)))
            except Exception:
                _log.exception("[Approval] notify event failed tool=%s", name)
            _log.info("[Approval] notify tool=%s", name)

        if tool_def.approval == APPROVAL_CONFIRM:
            if not self._confirm_callback:
                # 没有注册审批回调时返回 unavailable
                _log.warning(
                    "[Approval] unavailable tool=%s (no confirm callback)",
                    name,
                )
                approval_result = ApprovalDecision(
                    allowed=False, reason="unavailable"
                ).to_tool_result(name)
                return ToolResult.from_raw(name, approval_result)

            allowed = self._confirm_callback(name, input_data)
            if inspect.isawaitable(allowed):
                allowed = await allowed

            if not allowed:
                # 读取上一次决策的 reason（引擎中已设置 confirm_reason）
                approval_result = self._last_approval.to_tool_result(name)
                return ToolResult.from_raw(name, approval_result)

        # ── 执行 handler ────────────────────────────────────
        handler = tool_def.handler
        if inspect.iscoroutinefunction(handler):
            result = await handler(**input_data)
        else:
            result = handler(**input_data)
        return ToolResult.from_raw(name, result)

    def get_approval(self, name: str) -> str:
        """获取工具的 approval 级别。"""
        tool = self._tools.get(name)
        if tool is None:
            return APPROVAL_AUTO
        return tool.approval

    def list_tools(self, names: Optional[set[str] | list[str] | tuple[str, ...]] = None) -> list:
        """
        获取 Anthropic API 需要的工具定义列表

        Returns:
            工具定义列表（Anthropic format）
        """
        allowed_names = set(names) if names is not None else None
        tools = []
        for tool_def in self._tools.values():
            if allowed_names is not None and tool_def.name not in allowed_names:
                continue
            tools.append({
                "name": tool_def.name,
                "description": tool_def.description,
                "input_schema": tool_def.input_schema,
            })
        return tools

    def get_tool(self, name: str) -> Optional[ToolDef]:
        """获取单个工具定义"""
        return self._tools.get(name)

    def has_tool(self, name: str) -> bool:
        """检查工具是否已注册"""
        return name in self._tools

    def tool_count(self) -> int:
        """获取已注册工具数量"""
        return len(self._tools)

    def add_cleanup_hook(self, hook: Callable) -> None:
        """注册关闭钩子，用于释放工具层资源（如 MCP 子进程）。"""
        self._cleanup_hooks.append(hook)

    async def shutdown(self) -> None:
        """执行所有关闭钩子。"""
        for hook in self._cleanup_hooks:
            try:
                if inspect.iscoroutinefunction(hook):
                    await hook()
                else:
                    result = hook()
                    if inspect.isawaitable(result):
                        await result
            except Exception:
                pass

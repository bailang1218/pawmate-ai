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
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Dict, Any, Optional
import inspect
import asyncio
import json
from pawmate.core.safety.redaction import redact_for_json, redact_text
from pawmate.core.tools.tool_policy import PolicyDecision, evaluate_tool_policy

# ── 可选审批级别 ──────────────────────────────────────────────
APPROVAL_AUTO: str = "auto"       # 静默执行
APPROVAL_NOTIFY: str = "notify"   # 通知但不阻塞
APPROVAL_CONFIRM: str = "confirm" # 弹确认框
VALID_APPROVALS = (APPROVAL_AUTO, APPROVAL_NOTIFY, APPROVAL_CONFIRM)


class ToolCategory(str, Enum):
    BROWSER = "browser"
    FILE = "file"
    SHELL = "shell"
    MEMORY = "memory"
    SCHEDULER = "scheduler"
    NETWORK = "network"
    UI = "ui"
    SYSTEM = "system"
    DEBUG = "debug"
    GENERAL = "general"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SideEffectLevel(str, Enum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    DESTRUCTIVE = "destructive"
    PRIVILEGED = "privileged"


def _enum_value(enum_cls: type[Enum], value: Any, field_name: str, tool_name: str) -> str:
    raw = value.value if isinstance(value, enum_cls) else str(value or "").strip().lower()
    allowed = {item.value for item in enum_cls}
    if raw not in allowed:
        raise ValueError(f"tool '{tool_name}' has invalid {field_name}: {value}")
    return raw


def _validate_tool_input_schema(tool_name: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(schema, dict) or not schema:
        raise ValueError(f"tool '{tool_name}' requires a non-empty input_schema")
    if schema.get("type") != "object":
        raise ValueError(f"tool '{tool_name}' input_schema.type must be object")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError(f"tool '{tool_name}' input_schema.properties must be an object")
    required = schema.get("required", [])
    if required is None:
        required = []
        schema = {**schema, "required": required}
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ValueError(f"tool '{tool_name}' input_schema.required must be a list of strings")
    for required_name in required:
        if required_name not in properties:
            raise ValueError(f"tool '{tool_name}' requires unknown schema property: {required_name}")
    return schema


def _validate_tool_arguments(tool_def: "ToolDef", input_data: Dict[str, Any]) -> None:
    if not isinstance(input_data, dict):
        raise ValueError(f"tool '{tool_def.name}' arguments must be an object")
    try:
        encoded_size = len(json.dumps(input_data, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"tool '{tool_def.name}' arguments must be JSON serializable") from exc
    if encoded_size > 64 * 1024:
        raise ValueError(f"tool '{tool_def.name}' arguments exceed 64 KiB")
    schema = tool_def.input_schema
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(input_data) - set(properties))
        if unknown:
            raise ValueError(f"tool '{tool_def.name}' received unknown arguments: {', '.join(unknown)}")
    for required_name in schema.get("required", []) or []:
        if required_name not in input_data:
            raise ValueError(f"tool '{tool_def.name}' missing required argument: {required_name}")
    for key, value in input_data.items():
        prop_schema = properties.get(key)
        if not isinstance(prop_schema, dict):
            continue
        _validate_json_value(tool_def.name, key, value, prop_schema)


def _validate_json_value(tool_name: str, key: str, value: Any, schema: Dict[str, Any]) -> None:
    expected_type = schema.get("type")
    if expected_type is None:
        return
    if isinstance(expected_type, list):
        if any(_json_type_matches(value, item) for item in expected_type):
            return
        raise ValueError(f"tool '{tool_name}' argument '{key}' has invalid type")
    if not _json_type_matches(value, expected_type):
        raise ValueError(f"tool '{tool_name}' argument '{key}' must be {expected_type}")
    if expected_type == "string":
        max_length = int(schema.get("maxLength", 16 * 1024))
        if len(value) > max_length:
            raise ValueError(f"tool '{tool_name}' argument '{key}' exceeds maxLength")
    if expected_type == "array":
        max_items = int(schema.get("maxItems", 1000))
        if len(value) > max_items:
            raise ValueError(f"tool '{tool_name}' argument '{key}' exceeds maxItems")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_json_value(tool_name, f"{key}[{index}]", item, item_schema)
    if expected_type == "object" and isinstance(schema.get("properties"), dict):
        nested = ToolDef.__new__(ToolDef)
        nested.name = tool_name
        nested.input_schema = schema
        _validate_tool_arguments(nested, value)
    if "enum" in schema and value not in schema.get("enum", []):
        raise ValueError(f"tool '{tool_name}' argument '{key}' must be one of {schema.get('enum')}")
    if expected_type in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"tool '{tool_name}' argument '{key}' is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"tool '{tool_name}' argument '{key}' is above maximum")


def _json_type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "null":
        return value is None
    return True


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
    """Runtime tool definition with governance metadata."""
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable
    output_schema: Optional[Dict[str, Any]] = None
    category: ToolCategory | str = ToolCategory.GENERAL
    risk: RiskLevel | str = RiskLevel.LOW
    side_effect: SideEffectLevel | str = SideEffectLevel.READ_ONLY
    timeout: float = 60.0
    model_visible: bool = True
    requires_confirm: Optional[bool] = None
    permissions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    approval: str = APPROVAL_AUTO
    source: str = "builtin"
    runtime_policy: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise ValueError("tool name is required")
        if not callable(self.handler):
            raise ValueError(f"tool '{self.name}' handler must be callable")
        if self.runtime_policy is not None and not callable(self.runtime_policy):
            raise ValueError(f"tool '{self.name}' runtime_policy must be callable")
        if self.approval not in VALID_APPROVALS:
            raise ValueError(f"tool '{self.name}' has invalid approval: {self.approval}")
        self.input_schema = _validate_tool_input_schema(self.name, self.input_schema)
        self.category = _enum_value(ToolCategory, self.category, "category", self.name)
        self.risk = _enum_value(RiskLevel, self.risk, "risk", self.name)
        self.side_effect = _enum_value(SideEffectLevel, self.side_effect, "side_effect", self.name)
        self.timeout = float(self.timeout or 0)
        if self.timeout <= 0:
            raise ValueError(f"tool '{self.name}' timeout must be positive")
        if self.requires_confirm is None:
            self.requires_confirm = self.approval == APPROVAL_CONFIRM
        if self.requires_confirm and self.approval != APPROVAL_CONFIRM:
            raise ValueError(f"tool '{self.name}' requires confirm but approval is {self.approval}")
        high_risk = self.risk in {RiskLevel.HIGH.value, RiskLevel.CRITICAL.value}
        side_effect_risk = self.side_effect in {
            SideEffectLevel.EXTERNAL_WRITE.value,
            SideEffectLevel.DESTRUCTIVE.value,
            SideEffectLevel.PRIVILEGED.value,
        }
        if (high_risk or side_effect_risk) and self.approval != APPROVAL_CONFIRM:
            raise ValueError(f"high-risk tool '{self.name}' must use confirm approval")
        if self.source.startswith(("plugin:", "mcp:")) and self.model_visible:
            self.approval = APPROVAL_CONFIRM
            self.requires_confirm = True


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


def _attach_execution_decisions(
    result: ToolResult,
    policy_decision: PolicyDecision,
    approval_decision: ApprovalDecision,
) -> ToolResult:
    metadata = dict(result.metadata)
    metadata["runtime_policy_decision"] = policy_decision
    metadata["runtime_approval_decision"] = approval_decision
    return replace(result, metadata=metadata)


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
        self._last_policy_decision: PolicyDecision = PolicyDecision(allowed=True, reason="startup")

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
        for tool_def in tools.values():
            self.register(tool_def)

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
        _validate_tool_arguments(tool_def, input_data)
        policy_decision = evaluate_tool_policy(tool_def, input_data)
        approval_decision = ApprovalDecision(allowed=True, reason="not_required")
        self._last_policy_decision = policy_decision
        if not policy_decision.allowed:
            _log.warning("[Policy] denied tool=%s reason=%s", name, policy_decision.reason)
            return _attach_execution_decisions(
                ToolResult.from_raw(name, policy_decision.to_tool_result(name)),
                policy_decision,
                ApprovalDecision(allowed=False, reason="policy_denied"),
            )

        # ── approval 检查 ───────────────────────────────────
        effective_approval = str(
            policy_decision.metadata.get("approval") or tool_def.approval
        )
        if effective_approval == APPROVAL_NOTIFY:
            try:
                from pawmate.bridge.contracts import ToolNotifyEvent
                from pawmate.bridge.event_bus import event_bus

                event_bus.publish(ToolNotifyEvent(name, redact_for_json(input_data)))
            except Exception:
                _log.exception("[Approval] notify event failed tool=%s", name)
            _log.info("[Approval] notify tool=%s", name)

        if policy_decision.requires_confirm:
            if not self._confirm_callback:
                # 没有注册审批回调时返回 unavailable
                _log.warning(
                    "[Approval] unavailable tool=%s (no confirm callback)",
                    name,
                )
                approval_result = ApprovalDecision(
                    allowed=False, reason="unavailable"
                ).to_tool_result(name)
                return _attach_execution_decisions(
                    ToolResult.from_raw(name, approval_result),
                    policy_decision,
                    ApprovalDecision(allowed=False, reason="unavailable"),
                )

            allowed = self._confirm_callback(name, input_data)
            if inspect.isawaitable(allowed):
                allowed = await allowed

            if not allowed:
                # 读取上一次决策的 reason（引擎中已设置 confirm_reason）
                approval_result = self._last_approval.to_tool_result(name)
                approval_decision = self._last_approval
                return _attach_execution_decisions(
                    ToolResult.from_raw(name, approval_result),
                    policy_decision,
                    approval_decision,
                )
            approval_decision = self._last_approval

        # ── 执行 handler ────────────────────────────────────
        handler = tool_def.handler
        result = await asyncio.wait_for(
            self._invoke_handler(handler, input_data),
            timeout=tool_def.timeout,
        )
        return _attach_execution_decisions(
            ToolResult.from_raw(name, result),
            policy_decision,
            approval_decision,
        )

    @staticmethod
    async def _invoke_handler(handler: Callable, input_data: Dict[str, Any]) -> Any:
        if inspect.iscoroutinefunction(handler):
            return await handler(**input_data)
        result = await asyncio.to_thread(handler, **input_data)
        if inspect.isawaitable(result):
            return await result
        return result

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
            if not tool_def.model_visible:
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

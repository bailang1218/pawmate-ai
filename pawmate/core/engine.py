"""
ReAct 引擎

实现标准的 Reasoning + Acting 循环，最多 10 轮
异步工厂模式初始化，自动加载 MCP 工具
"""
import asyncio
import json
import time
import logging
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Dict, List, Optional
from dataclasses import dataclass

from pawmate.core.confirm_gate import ConfirmGate
from pawmate.core.mcp_loader import load_mcp_tools
from pawmate.memory.long_term_memory_manager import LongTermMemoryManager
from pawmate.memory.memory_store import MemoryStore
from pawmate.context import ConversationContextManager
from pawmate.scheduling import init_scheduler
from pawmate.core.heartbeat import HeartbeatController
from pawmate.core.llm_factory import get_llm_client, get_llm_client_with_fallback, DEFAULT_SYSTEM_PROMPT
from pawmate.core.llm_provider import LLMProvider
from pawmate.core.llm_router import RouteChoice, classify_task_kind, resolve_llm_route_candidates
from pawmate.core.provider_runner import ProviderRunner
from pawmate.core.task_planner import TaskPlanner, Plan
from pawmate.core.tool_router import route_tools_for_turn
from pawmate.tools.registry import ToolRegistry
from pawmate.core.prompt_assembler import build_runtime_prompt
from pawmate.core.tool_parser import (
    is_truncated_tool_protocol,
    parse_textual_tool_call,
    strip_any_textual_tool_call_protocol,
)
from pawmate.core.tool_call_runner import ToolCallRunner
from pawmate.core.tool_result_budget import budget_tool_result
from pawmate.core.turn_context_builder import TurnContextBuilder
from pawmate.tools.builtin_gateway import register_builtin_tools
from pawmate.tools.scheduler_tools import register_scheduler_tools
from pawmate.storage.history_store import HistoryStore
from pawmate.bridge.contracts import (
    ErrorEvent,
    FinishedEvent,
    ProgressUpdateEvent,
    ScheduledFiredEvent,
    TextDeltaEvent,
    ToolDoneEvent,
    ToolErrorEvent,
    ToolStartEvent,
)
from pawmate.bridge.event_bus import event_bus
from pawmate.core.redaction import redact_for_json, redact_sensitive_data
from pawmate.core.security_service import SecurityService
import pawmate.config as config


_ACTIVE_TURN_ID: ContextVar[int] = ContextVar("pawmate_active_turn_id", default=0)
_CHAT_DEPTH: ContextVar[int] = ContextVar("pawmate_chat_depth", default=0)
DEFAULT_MAX_TOOL_CALLS_PER_TURN = 64
DEFAULT_BROWSER_MAX_TOOL_CALLS_PER_TURN = 128


@dataclass
class EngineState:
    """引擎运行状态"""
    turn: int = 0
    is_running: bool = False
    tool_call_history: List[Dict] = None  # 工具调用历史


@dataclass
class TurnCompletedContext:
    user_input: str
    final_response: str
    user_seq: int | None
    consolidate_context: bool


class AgentEngine:
    """
    ReAct 引擎：Reasoning + Acting 循环

    流程：
      1. 从 HistoryStore 读消息
      2. 调 LLMClient.stream() 获取响应
      3. 解析 tool_use block，调 ToolRegistry.execute()
      4. 结果写回 history，继续循环
      5. 最多 MAX_TURNS 轮，或 LLM 返回 end_turn

    初始化：
      使用异步工厂方法 create() 而不是 __init__()
      engine = await AgentEngine.create()
    """

    def __init__(
        self,
        llm_client: LLMProvider,
        tool_registry: ToolRegistry,
        history_store: HistoryStore,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        enable_task_planning: bool = True,
        current_provider: Optional[str] = None,
        app_config: Optional[dict] = None,
    ):
        """
        初始化引擎（内部使用，外部用 create()）

        Args:
            llm_client: LLM 提供商客户端
            tool_registry: 工具注册表
            history_store: 历史存储
            system_prompt: 系统提示词
            enable_task_planning: 是否启用 LLM 任务规划（复杂任务自动拆解）
        """
        self._llm = llm_client
        self._registry = tool_registry
        self._history = history_store
        self._system_prompt = system_prompt
        self._app_config = app_config or {}
        self._active_route_choice: Optional[RouteChoice] = None
        self._state = EngineState(tool_call_history=[])
        self._active_chat_count = 0
        self._tool_failure_streak: Dict[str, int] = {}
        self._recent_task_route_tool_names: set[str] = set()
        self._nested_task_route_tool_names: set[str] = set()
        self._task_planner = TaskPlanner(llm_client=llm_client) if enable_task_planning else None

        # 错误处理和重试配置
        self._max_retries = 3
        self._retry_delay = 1.0  # 秒
        self._parallel_tool_limit = 4
        self._tool_call_runner = ToolCallRunner(
            self._registry,
            max_retries=self._max_retries,
            retry_delay=self._retry_delay,
        )
        self._provider_runner = ProviderRunner(
            llm_client=self._llm,
            current_provider=current_provider,
        )
        self._chat_lock: asyncio.Lock | None = None
        self._chat_owner_task: asyncio.Task | None = None
        self._turn_completed_hooks: List[
            Callable[[TurnCompletedContext], Awaitable[None]]
        ] = []
        self._register_turn_completed_hook(self._record_successful_turn_on_completed)
        self._register_turn_completed_hook(self._consolidate_context_on_completed)
        self._turn_context_builder = TurnContextBuilder(
            static_prompt_provider=lambda: getattr(self, "_static_prompt", self._system_prompt),
            conversation_context_provider=lambda: self._conversation_context,
            long_term_memory_provider=lambda: self._long_term_memory,
        )

        # ── 存储 worker 线程的 event loop 引用（用于跨线程唤醒 asyncio 对象）──
        self._worker_loop: Optional[asyncio.AbstractEventLoop] = None

        # ── 上下文窗口限制 ─────────────────────────────────────
        self._max_context_chars = self._get_max_context_chars()

        # ── 桌宠心跳 ──────────────────────────────────────────
        self._heartbeat = HeartbeatController(
            llm_client=self._llm,
            is_engine_running=lambda: self._state.is_running,
        )

        # ── 工具确认门 ──────────────────────────────────────
        self._confirm_gate = ConfirmGate(
            tool_registry=self._registry,
            event_bus=event_bus,
        )

    @property
    def long_term_memory(self):
        return self._long_term_memory


    @classmethod
    async def create(
        cls,
        llm_client: Optional[LLMProvider] = None,
        history_store: Optional[HistoryStore] = None,
        system_prompt: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        app_config: Optional[dict] = None,
    ) -> "AgentEngine":
        """
        异步工厂方法：初始化引擎并加载 MCP 工具

        Args:
            llm_client: 已初始化的 LLM 客户端（可选，不传则自动创建）
            history_store: 历史存储（可选，不传则新建）
            system_prompt: 自定义 system prompt（可选）
                不传且传了 app_config 则自动从 PromptAssembler 构建
                不传且没有 app_config 则回退到 DEFAULT_SYSTEM_PROMPT
            provider: LLM 提供商（自动创建客户端时使用）
            model: LLM 模型名（自动创建客户端时使用）
            app_config: 应用配置字典（用于自动构建 prompt）
        """
        import logging
        _log = logging.getLogger("pawmate")

        # 创建 LLM 客户端（如果未提供），带 fallback 链
        _current_provider_name = provider  # 记录当前使用的提供商名
        if llm_client is None:
            _log.info("[AgentEngine] provider=%s model=%s", provider or "(default)", model or "(default)")
            try:
                if provider:
                    # 单提供商创建（由调用方决定是否走 fallback）
                    llm_client = get_llm_client(provider=provider, model=model)
                else:
                    # 走 fallback 链自动选择
                    _current_provider_name, llm_client = get_llm_client_with_fallback(
                        primary_provider=provider, model=model,
                    )
            except RuntimeError as e:
                raise RuntimeError(f"LLM 初始化失败: {e}") from e

        _log.info("[AgentEngine] engine created with %s/%s",
                  _current_provider_name or getattr(llm_client, "_provider", "?"),
                  getattr(llm_client, "_model", "?"))

        if history_store is None:
            history_store = HistoryStore()

        # 创建工具注册表并加载内置网关工具
        tool_registry = ToolRegistry()
        security_service = SecurityService()
        register_builtin_tools(tool_registry, security_service=security_service)

        # 加载 MCP server（委派到 mcp_loader）
        count = await load_mcp_tools(tool_registry)
        if count is not None:
            _log.info("[MCP] 成功加载 %d 个 MCP 工具", count)

        # 记忆系统初始化（v2：长期记忆 + 对话上下文分离）
        store = MemoryStore()
        ltm = LongTermMemoryManager(
            store,
            user_id="local-default",
            companion_id="default-pet",
        )
        initial_session_id = getattr(history_store, "session_id", getattr(history_store, "_sid", ""))
        ltm.set_current_conversation(initial_session_id)
        conversation_id = initial_session_id or "default-session"
        ctx = ConversationContextManager(
            history_store=history_store,
            meta_store=store,
            conversation_id=conversation_id,
        )
        ctx.attach_llm(llm_client)
        from pawmate.tools.memory_tools import register_memory_tools
        register_memory_tools(tool_registry, ltm)

        # 退役旧运行时记忆文件（USER.md / MEMORY.md → SQLite）
        _retire_legacy_markdown(store, ltm)

        # 定时任务系统初始化（委派到 scheduling 模块）
        scheduler = init_scheduler()
        if scheduler is not None:
            register_scheduler_tools(tool_registry, scheduler)

        # Prompt 组装（静态部分：身份+规则；动态记忆块每轮注入）
        static_prompt = build_runtime_prompt(
            tool_registry=tool_registry,
            core_mem=None,  # Dynamic memory is injected every turn by LongTermMemoryManager.
            system_prompt=system_prompt,
            app_config=app_config,
        )

        # 创建引擎实例
        engine = cls(
            llm_client=llm_client,
            tool_registry=tool_registry,
            history_store=history_store,
            system_prompt=static_prompt,
            current_provider=_current_provider_name,
            app_config=app_config,
        )
        security_service.set_confirm_gate(engine._confirm_gate)
        engine._long_term_memory = ltm
        engine._conversation_context = ctx
        engine._static_prompt = static_prompt
        engine._heartbeat.set_memory_context_provider(
            lambda: ltm.build_memory_block("心跳 闲置提醒 称呼 名字 PawMate 陪伴风格")
        )
        engine.apply_approval_config(app_config)

        # ── 注册工具确认回调 ────────────────────────────
        engine._confirm_gate.setup()

        # ── 定时任务触发回调 ────────────────────────────────
        engine._scheduler = scheduler
        if scheduler is not None:
            async def _on_scheduled_fire(task_id: str, action: str) -> None:
                if engine._state.is_running:
                    _log.warning("[Scheduler] engine busy, drop task %s", task_id)
                    return
                framed = (
                    f"[Scheduled task fired: {task_id}]\n"
                    f"Action to perform now: {action}\n\n"
                    f"Follow this instruction and briefly tell the user the result."
                )
                try:
                    event_bus.publish(ScheduledFiredEvent(task_id, action))
                except Exception:
                    pass
                try:
                    await engine.chat(framed, emit_finished=True)
                except Exception:
                    _log.exception("[Scheduler] task %s execution failed", task_id)

            scheduler.set_callback(_on_scheduled_fire)
            scheduler.start()

        # 注入 engine 引用到 ConfigBridge（供 settings 页查询记忆状态）
        try:
            from pawmate.bridge.config_bridge import inject_engine
            inject_engine(engine, bridge=None)
        except Exception:
            pass

        return engine

    # ============================================================
    # 工具确认（委派到 ConfirmGate）
    # ============================================================

    def replace_history_session(self, session_id: str) -> None:
        self._history.replace_session(session_id)
        context = getattr(self, "_conversation_context", None)
        if context is not None and hasattr(context, "conversation_id"):
            context.conversation_id = session_id
        self.set_active_session(session_id)

    def get_active_session_id(self) -> str:
        return getattr(self._history, "session_id", getattr(self._history, "_sid", ""))

    def _get_active_turn_id(self) -> int:
        return _ACTIVE_TURN_ID.get()

    def apply_approval_config(self, app_config: Optional[dict]) -> None:
        if _approval_llm_explain_enabled(app_config):
            self._confirm_gate.set_explanation_provider(
                lambda tool_name, tool_input: _explain_tool_approval(
                    self._llm,
                    tool_name,
                    tool_input,
                )
            )
        else:
            self._confirm_gate.set_explanation_provider(None)
        self._confirm_gate.set_auto_approve(_approval_auto_approve_enabled(app_config))

    def get_history_messages(self) -> List[Dict[str, Any]]:
        return self._history.get_messages()

    def get_history_session_store(self) -> Any:
        return self._history.session_store

    def set_active_session(self, session_id: str) -> None:
        if (
            hasattr(self, "_long_term_memory")
            and hasattr(self._long_term_memory, "set_current_conversation")
        ):
            self._long_term_memory.set_current_conversation(session_id or "")

    def _sync_memory_conversation_source(
        self,
        start_seq: Optional[int] = None,
        end_seq: Optional[int] = None,
    ) -> None:
        if (
            not hasattr(self, "_long_term_memory")
            or not hasattr(self._long_term_memory, "set_current_conversation")
        ):
            return
        session_id = self.get_active_session_id()
        if end_seq is None:
            try:
                end_seq = self._history.snapshot()
            except Exception:
                end_seq = start_seq
        self._long_term_memory.set_current_conversation(
            session_id,
            start_seq=start_seq,
            end_seq=end_seq,
        )

    def resolve_confirm(self, allowed: bool) -> None:
        """从 UI 线程调用，委派到 ConfirmGate。"""
        self._confirm_gate.resolve(allowed)

    def _compose_runtime_prompt(self, user_message: str = "") -> str:
        """每轮动态组装运行时 system prompt：静态身份 + 对话上下文 + 长期记忆。"""
        prompt = self._turn_context_builder.compose_runtime_prompt(user_message)
        route = getattr(self, "_active_route_choice", None)
        if route is None:
            return prompt
        model = route.model or "(default)"
        return (
            f"{prompt}\n\n"
            "[LLM route]\n"
            f"Current route: {route.kind} -> {route.provider}/{model}\n"
            "Do not claim to be another model/provider."
        )

    def _route_llm_for_turn(self, user_input: str) -> None:
        if not self._app_config:
            return

        route_kind = classify_task_kind(user_input)
        choices = resolve_llm_route_candidates(
            route_kind,
            self._app_config,
            user_input=user_input,
        )
        if not choices:
            return

        route = choices[0]
        self._provider_runner.activate_route(route, choices)
        self._llm = self._provider_runner.llm_client
        self._active_route_choice = route

        if self._task_planner is not None:
            self._task_planner.llm_client = self._llm

        context = getattr(self, "_conversation_context", None)
        attach_llm = getattr(context, "attach_llm", None)
        if callable(attach_llm):
            attach_llm(self._llm)

        logging.getLogger("pawmate").info(
            "[LLMRouter] %s -> %s/%s (%s)",
            route.kind,
            route.provider,
            route.model or "(default)",
            route.reason,
        )

    def _register_turn_completed_hook(
        self,
        hook: Callable[[TurnCompletedContext], Awaitable[None]],
    ) -> None:
        self._turn_completed_hooks.append(hook)

    async def _emit_turn_completed(self, context: TurnCompletedContext) -> None:
        for hook in list(self._turn_completed_hooks):
            await hook(context)

    async def _record_successful_turn_on_completed(
        self,
        context: TurnCompletedContext,
    ) -> None:
        await self._record_successful_turn(
            context.user_input,
            context.final_response,
            context.user_seq,
        )

    async def _consolidate_context_on_completed(
        self,
        context: TurnCompletedContext,
    ) -> None:
        if not context.consolidate_context:
            return
        consolidate = getattr(self._conversation_context, "maybe_consolidate", None)
        if consolidate is not None:
            await consolidate()

    async def _record_successful_turn(
        self,
        user_input: str,
        final_response: str,
        user_seq: int | None,
    ) -> None:
        manager = getattr(self, "_long_term_memory", None)
        callback = getattr(manager, "on_successful_turn_end", None)
        if callback is None:
            return

        try:
            end_seq = self._history.snapshot()
        except Exception:
            end_seq = user_seq

        try:
            await callback(
                user_message=user_input,
                assistant_response=final_response,
                session_id=self.get_active_session_id(),
                start_seq=user_seq,
                end_seq=end_seq,
            )
        except TypeError:
            await callback()
        except Exception as exc:
            logging.getLogger("pawmate").warning(
                "[Memory] successful-turn callback failed: %s",
                exc,
            )

    def _get_max_turns(self) -> int:
        """读取 config.json runtime.max_turns，fallback 到 config.MAX_TURNS。"""
        try:
            import json
            cfg = json.loads((config.BASE_DIR / "config.json").read_text("utf-8"))
            return int(cfg.get("runtime", {}).get("max_turns", config.MAX_TURNS))
        except Exception:
            return config.MAX_TURNS

    def _get_max_tool_calls_per_turn(self, route_context: str = "") -> int:
        runtime_cfg = self._app_config.get("runtime", {}) if isinstance(self._app_config, dict) else {}
        try:
            value = runtime_cfg.get("max_tool_calls_per_turn")
            if value is None:
                cfg = json.loads((config.BASE_DIR / "config.json").read_text("utf-8"))
                value = cfg.get("runtime", {}).get(
                    "max_tool_calls_per_turn",
                    DEFAULT_MAX_TOOL_CALLS_PER_TURN,
                )
            limit = max(16, int(value))
        except Exception:
            limit = DEFAULT_MAX_TOOL_CALLS_PER_TURN
        if self._looks_like_browser_task(route_context):
            return max(DEFAULT_BROWSER_MAX_TOOL_CALLS_PER_TURN, limit)
        return limit

    def _get_max_context_chars(self) -> int:
        try:
            cfg = json.loads((config.BASE_DIR / "config.json").read_text("utf-8"))
            value = int(cfg.get("runtime", {}).get("max_context_chars", 48000))
            return max(4000, value)
        except Exception:
            return 48000

    def _get_messages_context_budget(
        self,
        system_prompt: str,
        tools_defs: List[Dict[str, Any]],
    ) -> int:
        try:
            tools_chars = len(json.dumps(tools_defs, ensure_ascii=False, default=str))
        except Exception:
            tools_chars = 0
        overhead = len(system_prompt or "") + tools_chars + 1000
        return max(2000, self._max_context_chars - overhead)

    def _apply_tool_budget_prompt(
        self,
        system_prompt: str,
        tool_calls_used: int,
        max_tool_calls: int,
    ) -> str:
        if tool_calls_used < max_tool_calls:
            return system_prompt
        return (
            f"{system_prompt}\n\n"
            "[Tool safety limit]\n"
            f"You have already used {tool_calls_used} tool call(s), which reaches the hard safety "
            f"limit of {max_tool_calls}. Do not call tools again in this turn. Use the existing "
            "tool results and answer the user directly. If the task is incomplete, say exactly "
            "what is still missing and what you already verified."
        )

    @staticmethod
    def _looks_like_browser_task(text: str) -> bool:
        lowered = (text or "").lower()
        return any(
            marker in lowered
            for marker in (
                "browser",
                "native_browser",
                "edge",
                "chrome",
                "http://",
                "https://",
                "浏览器",
                "网页",
                "网站",
                "抖音",
                "淘宝",
                "小红书",
            )
        )

    def _build_tool_route_context(self, user_input: str) -> str:
        parts = [user_input or ""]
        try:
            recent_messages = self._history.get_messages_windowed(max_chars=8000)[-10:]
        except Exception:
            recent_messages = []
        last_user_idx = None
        for idx, message in enumerate(recent_messages):
            if message.get("role") == "user":
                last_user_idx = idx
        if last_user_idx is not None:
            current_user_content = str(recent_messages[last_user_idx].get("content") or "").strip()
            if current_user_content == (user_input or "").strip():
                recent_messages = recent_messages[last_user_idx:]
            else:
                recent_messages = []
        for message in recent_messages:
            role = str(message.get("role") or "")
            content = message.get("content", "")
            if isinstance(content, (dict, list)):
                try:
                    content_text = json.dumps(content, ensure_ascii=False, default=str)
                except Exception:
                    content_text = str(content)
            else:
                content_text = str(content)
            if content_text:
                parts.append(f"{role}: {content_text}")
        return "\n".join(part for part in parts if part)

    def _normalize_route_tool_names(self, tool_names: set[str]) -> set[str]:
        available = {tool["name"] for tool in self._registry.list_tools()}
        return {
            str(name or "").strip()
            for name in tool_names
            if str(name or "").strip() in available
        }

    def _remember_recent_task_route(self, tool_names: set[str]) -> None:
        route_tools = self._normalize_route_tool_names(tool_names)
        if not route_tools:
            return
        self._recent_task_route_tool_names = route_tools
        logging.getLogger("pawmate").info(
            "[ToolSticky] remembered route tools=%d",
            len(route_tools),
        )

    def _get_chat_lock(self) -> asyncio.Lock:
        if self._chat_lock is None:
            self._chat_lock = asyncio.Lock()
        return self._chat_lock

    async def chat(self, user_input: str, emit_finished: bool = True, turn_id: int = 0) -> str:
        return await self._chat_unlocked(user_input, emit_finished=emit_finished, turn_id=turn_id)

    async def _chat_unlocked(self, user_input: str, emit_finished: bool = True, turn_id: int = 0) -> str:
        """
        聊天入口：用户输入 -> ReAct 循环 -> 最终回复

        Args:
            user_input: 用户输入文本

        Returns:
            最终的助手回复文本

        发出信号（通过 EventBus）：
          - text_delta(str): 流式文本片段
          - tool_start(tool_name, input_json): 工具开始调用
          - tool_done(tool_name, result): 工具调用完毕
          - finished(): 交互完毕
          - error(msg): 发生错误
        """
        # 用户说话了，重置心跳计时
        self._heartbeat.poke()

        entering_depth = _CHAT_DEPTH.get()
        depth_token = _CHAT_DEPTH.set(entering_depth + 1)
        turn_token = None
        if entering_depth == 0:
            self._active_chat_count += 1
            self._state.is_running = True
            self._tool_failure_streak.clear()
            turn_token = _ACTIVE_TURN_ID.set(int(turn_id or 0))
        turn_count = 0
        final_response = ""

        _log = __import__('logging').getLogger("pawmate")

        # ★ snapshot：仅在最外层 chat() 记录，嵌套调用（如 TODO 分步）不覆盖
        if entering_depth == 0:
            self._turn_snapshot_seq = self._history.snapshot()
            self._nested_task_route_tool_names.clear()
            logging.getLogger("pawmate").info("[Turn] engine chat started")

        try:
            if entering_depth == 0:
                self._route_llm_for_turn(user_input)

            # 1. 追加用户消息
            user_seq = self._history.append_user(user_input)
            self._sync_memory_conversation_source(
                start_seq=user_seq,
                end_seq=self._history.snapshot(),
            )

            # 2. 尝试任务规划
            if self._task_planner and emit_finished:
                plan = await self._task_planner.plan_with_llm(user_input)
                if plan is not None and len(plan.tasks) > 1:
                    result = await self._execute_todo_steps(user_input, plan)
                    if entering_depth == 0:
                        self._remember_recent_task_route(self._nested_task_route_tool_names)
                    return await self._finish_successful_chat_turn(
                        user_input,
                        result,
                        user_seq,
                        emit_finished=emit_finished,
                        consolidate_context=False,
                    )

            # 3. ReAct 循环
            route_context = self._build_tool_route_context(user_input)
            max_tool_calls = self._get_max_tool_calls_per_turn(route_context)
            tool_calls_used = 0
            task_route_tool_names: set[str] = set(self._recent_task_route_tool_names)
            truncation_retries_left = 2
            while turn_count < self._get_max_turns():
                turn_count += 1
                self._sync_memory_conversation_source(
                    start_seq=user_seq,
                    end_seq=self._history.snapshot(),
                )

                # 调 LLM，获取响应（带上下文窗口限制）
                current_text = ""
                had_tool_use = False
                tool_use_events: List[Dict[str, Any]] = []
                tool_call_requests: List[Dict[str, Any]] = []
                pending_tool_results: List[Dict[str, str]] = []
                remaining_tool_calls = max(0, max_tool_calls - tool_calls_used)
                tool_safety_limit_reached = remaining_tool_calls <= 0
                route_context = self._build_tool_route_context(user_input)
                if tool_safety_limit_reached:
                    tool_route = None
                    tools_defs = []
                else:
                    tool_route = route_tools_for_turn(
                        route_context,
                        (tool["name"] for tool in self._registry.list_tools()),
                        sticky_tools=task_route_tool_names or None,
                    )
                    if tool_route.tool_names:
                        task_route_tool_names = set(tool_route.tool_names)
                        _log.info(
                            "[ToolRoute] kind=%s matched=%d inherited=%d tools=%d",
                            tool_route.kind,
                            len(tool_route.matched_tool_names),
                            len(tool_route.inherited_tool_names),
                            len(tool_route.tool_names),
                        )
                    tools_defs = self._registry.list_tools(tool_route.tool_names) if tool_route.tool_names else []
                system_prompt = self._compose_runtime_prompt(user_input)
                system_prompt = self._apply_tool_budget_prompt(
                    system_prompt,
                    tool_calls_used,
                    max_tool_calls,
                )
                messages_budget = self._get_messages_context_budget(system_prompt, tools_defs)
                messages = self._history.get_messages_windowed(max_chars=messages_budget)

                final_response_before_stream = final_response
                current_text, final_response, stream_events = await self._provider_runner.collect_stream_events(
                    messages=messages,
                    tools_defs=tools_defs,
                    protocol_filter_tools_defs=self._registry.list_tools(),
                    final_response=final_response,
                    system_prompt_factory=lambda prompt=system_prompt: prompt,
                    turn_id=self._get_active_turn_id(),
                    max_tokens=8192,
                )
                response_for_history = self._provider_runner.get_last_response()

                # 处理收集到的流式事件
                dropped_tool_calls = 0
                for event in stream_events:
                    if event["type"] == "text_delta":
                        continue

                    elif event["type"] == "tool_use":
                        if remaining_tool_calls <= 0:
                            dropped_tool_calls += 1
                            response_for_history = None
                            continue
                        remaining_tool_calls -= 1
                        had_tool_use = True
                        tool_name = event["name"]
                        tool_input = event.get("input", {})
                        tool_id = event.get("id", "")

                        tool_use_events.append({
                            "id": tool_id,
                            "name": tool_name,
                            "input": tool_input,
                        })

                        tool_call_requests.append({
                            "tool_name": tool_name,
                            "tool_input": tool_input,
                            "tool_id": tool_id,
                            "log_gateway_start": True,
                        })

                if dropped_tool_calls:
                    _log.warning(
                        "[ToolBudget] dropped %d tool call(s) after per-turn limit %d",
                        dropped_tool_calls,
                        max_tool_calls,
                    )

                if tool_call_requests:
                    await self._handle_tool_call_batch(tool_call_requests, pending_tool_results)
                    tool_calls_used += len(tool_call_requests)

                # 如果模型输出的是文本型伪调用，把它转换成真实工具调用。
                if not had_tool_use and current_text:
                    textual_call = parse_textual_tool_call(current_text, self._registry)
                    if (
                        textual_call is not None
                        and remaining_tool_calls > 0
                        and tool_route is not None
                        and textual_call["name"] in tool_route.tool_names
                    ):
                        had_tool_use = True
                        remaining_tool_calls -= 1
                        original_current_text = current_text
                        current_text = str(textual_call.get("visible_text") or "")
                        final_response = self._clean_textual_tool_call_response(
                            final_response,
                            original_current_text,
                            current_text,
                            str(textual_call.get("call_text") or ""),
                        )
                        response_for_history = None
                        tool_name = textual_call["name"]
                        tool_input = textual_call["input"]
                        tool_id = f"synthetic_{int(time.time() * 1000)}"

                        tool_use_events.append({
                            "id": tool_id,
                            "name": tool_name,
                            "input": tool_input,
                        })

                        tool_result_ui = await self._handle_tool_call_event(
                            tool_name,
                            tool_input,
                            tool_id,
                            pending_tool_results,
                            synthetic_tool_call=True,
                        )
                        if tool_result_ui is not None:
                            if final_response:
                                final_response = f"{final_response}\n\n[工具结果] {tool_result_ui}"
                            else:
                                final_response = tool_result_ui
                        tool_calls_used += 1
                    else:
                        sanitized_current_text = strip_any_textual_tool_call_protocol(current_text)
                        if sanitized_current_text != current_text:
                            final_response = self._clean_textual_tool_call_response(
                                final_response,
                                current_text,
                                sanitized_current_text,
                                "",
                            )
                            current_text = sanitized_current_text
                            response_for_history = None

                if (
                    not had_tool_use
                    and current_text
                    and truncation_retries_left > 0
                    and is_truncated_tool_protocol(current_text, self._registry)
                ):
                    truncation_retries_left -= 1
                    final_response = final_response_before_stream
                    response_for_history = None
                    _log.warning(
                        "[ToolProtocol] truncated textual tool protocol detected; retrying turn (%d left)",
                        truncation_retries_left,
                    )
                    continue

                assistant_content = self._build_assistant_history_content(
                    response_for_history,
                    current_text,
                    tool_use_events,
                )
                self._append_assistant_and_tool_results(
                    assistant_content,
                    pending_tool_results,
                )

                # 本轮无工具调用则结束循环
                if not had_tool_use:
                    break

            # 3. 完成
            if task_route_tool_names:
                if entering_depth == 0:
                    self._remember_recent_task_route(task_route_tool_names)
                else:
                    self._nested_task_route_tool_names.update(task_route_tool_names)
            return await self._finish_successful_chat_turn(
                user_input,
                final_response,
                user_seq,
                emit_finished=emit_finished,
                consolidate_context=True,
            )

        except asyncio.CancelledError:
            logging.getLogger("pawmate").info("[Turn] engine chat cancelled")
            raise

        except Exception as e:
            logging.getLogger("pawmate").error("[Turn] engine chat error: %s", e)
            error_msg = f"引擎错误: {str(e)}"
            event_bus.publish(ErrorEvent(error_msg, turn_id=self._get_active_turn_id()))
            raise

        finally:
            _CHAT_DEPTH.reset(depth_token)
            if turn_token is not None:
                _ACTIVE_TURN_ID.reset(turn_token)
            if entering_depth == 0:
                self._active_chat_count = max(0, self._active_chat_count - 1)
                self._state.is_running = self._active_chat_count > 0

    def cancel_current_turn(self) -> int:
        """
        中断当前任务，不删除历史。

        旧逻辑会 rollback 到 turn 开始前，取消一次任务就可能把当前会话里
        已经展示/写入的消息删掉。取消应当是运行时控制，不应当是破坏性撤销。
        返回值保留给 UI 兼容，固定为 0（删除消息数）。
        """
        _log = logging.getLogger("pawmate")

        # 防御：如果 already not running，no-op
        if not self._state.is_running:
            _log.warning("[Engine] cancel_current_turn called but not running — no-op")
            return 0

        self._turn_snapshot_seq = None
        self._state.is_running = False

        # 取消正在等待的工具确认（委派给 ConfirmGate，无待确认时安全 no-op）
        self._confirm_gate.resolve(False)

        _log = logging.getLogger("pawmate")
        _log.info("[Engine] cancelled without deleting history")
        return 0

    async def _handle_tool_call_batch(
        self,
        tool_calls: List[Dict[str, Any]],
        pending_tool_results: List[Dict[str, str]],
    ) -> None:
        if not tool_calls:
            return
        if len(tool_calls) <= 1 or self._parallel_tool_limit <= 1:
            for call in tool_calls:
                await self._handle_tool_call_event(
                    call["tool_name"],
                    call["tool_input"],
                    call["tool_id"],
                    pending_tool_results,
                    log_gateway_start=bool(call.get("log_gateway_start")),
                )
            return

        semaphore = asyncio.Semaphore(self._parallel_tool_limit)

        async def run_one(call: Dict[str, Any]) -> List[Dict[str, str]]:
            local_pending: List[Dict[str, str]] = []
            async with semaphore:
                await self._handle_tool_call_event(
                    call["tool_name"],
                    call["tool_input"],
                    call["tool_id"],
                    local_pending,
                    log_gateway_start=bool(call.get("log_gateway_start")),
                )
            return local_pending

        batches = await asyncio.gather(*(run_one(call) for call in tool_calls))
        for local_pending in batches:
            pending_tool_results.extend(local_pending)

    async def _handle_tool_call_event(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        tool_id: str,
        pending_tool_results: List[Dict[str, str]],
        *,
        synthetic_tool_call: bool = False,
        log_gateway_start: bool = False,
    ) -> Optional[str]:
        _log = logging.getLogger("pawmate")
        event_bus.publish(ToolStartEvent(
            tool_name,
            self._build_tool_card_payload(tool_name, tool_input, stage="start"),
            turn_id=self._get_active_turn_id(),
        ))
        if log_gateway_start:
            _log.info("[GATEWAY] 调用工具: %s", tool_name)

        failure_signature = self._tool_failure_signature(tool_name, tool_input)
        if self._tool_failure_streak.get(failure_signature, 0) >= 3:
            message = (
                f"[tool_repeated_failure] {tool_name} with the same key arguments has failed 3 times. "
                "Stop retrying this exact call; choose another tool, wait for a different condition, "
                "use returned candidates, or ask the user for clarification."
            )
            views = budget_tool_result(tool_name, message, session_id=self.get_active_session_id())
            event_bus.publish(ToolErrorEvent(
                tool_name,
                self._build_tool_card_payload(
                    tool_name,
                    tool_input,
                    stage="error",
                    result=views.ui,
                    success=False,
                ),
                turn_id=self._get_active_turn_id(),
            ))
            self._queue_pending_tool_result(pending_tool_results, tool_id, tool_name, views.model)
            self._record_tool_call_history(
                tool_name,
                tool_input,
                views,
                success=False,
                synthetic_tool_call=synthetic_tool_call,
            )
            return None

        outcome = await self._tool_call_runner.run(
            tool_name,
            tool_input,
            session_id=self.get_active_session_id(),
        )
        tool_result_failed = (not outcome.success) or self._tool_result_indicates_failure(outcome.views.model)
        self._record_tool_failure_streak(failure_signature, tool_result_failed)
        if outcome.success:
            if log_gateway_start:
                _log.info("[GATEWAY] %s completed (%.1fs)", tool_name, outcome.elapsed)

            event_bus.publish(ToolDoneEvent(
                tool_name,
                self._build_tool_card_payload(
                    tool_name,
                    tool_input,
                    stage="done",
                    result=outcome.views.ui,
                    success=True,
                ),
                turn_id=self._get_active_turn_id(),
            ))

            self._queue_pending_tool_result(
                pending_tool_results,
                tool_id,
                tool_name,
                outcome.views.model,
            )
            self._record_tool_call_history(
                tool_name,
                tool_input,
                outcome.views,
                synthetic_tool_call=synthetic_tool_call,
            )
            return outcome.views.ui

        _log.error("[GATEWAY] %s failed: %s", tool_name, outcome.error)
        event_bus.publish(ToolErrorEvent(
            tool_name,
            self._build_tool_card_payload(
                tool_name,
                tool_input,
                stage="error",
                result=outcome.views.ui,
                success=False,
            ),
            turn_id=self._get_active_turn_id(),
        ))

        self._queue_pending_tool_result(
            pending_tool_results,
            tool_id,
            tool_name,
            outcome.views.model,
        )
        self._record_tool_call_history(
            tool_name,
            tool_input,
            outcome.views,
            success=False,
            synthetic_tool_call=synthetic_tool_call,
        )
        return None

    def _build_tool_card_payload(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        *,
        stage: str,
        result: Optional[str] = None,
        success: Optional[bool] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "__pawmate_tool_card": True,
            "tool_input": tool_input,
            "narration": self._build_tool_card_narration(
                tool_name,
                tool_input,
                stage=stage,
                result=result,
                success=success,
            ),
        }
        if result is not None:
            payload["tool_result"] = result
        return redact_for_json(payload)

    def _build_tool_card_narration(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        *,
        stage: str,
        result: Optional[str] = None,
        success: Optional[bool] = None,
    ) -> Dict[str, str]:
        tool_def = self._registry.get_tool(tool_name)
        description = (getattr(tool_def, "description", "") or "").strip()
        title = description.splitlines()[0].strip(" .。") if description else tool_name
        target = self._tool_card_target(tool_input)

        narration = {
            "title": title[:48] or tool_name,
            "intent": description[:180] if description else f"执行工具 {tool_name}。",
            "plan": "先执行这个工具，再根据真实返回继续判断下一步。",
            "result": "工具正在执行，原始参数保留在详情里。",
        }
        if target:
            narration["intent"] = f"{narration['intent']} 目标：{target}"[:220]

        if stage == "done":
            narration["result"] = "工具已经返回结果，详情里保留真实输出。"
            if result and "页面内容" in result and "(空)" in result:
                narration["result"] = (
                    "页面已打开，但正文抽取为空；常见原因是脚本渲染、登录墙、反爬或页面本身没有可读正文。"
                )
        elif stage == "error":
            narration["title"] = f"{narration['title']}失败"[:48]
            narration["result"] = "工具调用失败，错误详情保留在展开区域。"
        if success is False:
            narration["plan"] = "先根据错误类型调整方式，再继续推进任务。"
        return narration

    @staticmethod
    def _tool_card_target(tool_input: Dict[str, Any]) -> str:
        for key in ("url", "target", "query", "text", "path", "file", "name"):
            value = tool_input.get(key)
            if value:
                text = str(value).replace("\n", " ").strip()
                return text[:120]
        return ""

    def _queue_pending_tool_result(
        self,
        pending_tool_results: List[Dict[str, str]],
        tool_id: str,
        tool_name: str,
        result: str,
    ) -> None:
        if tool_id:
            if self._is_browser_or_vision_tool(tool_name):
                for item in pending_tool_results:
                    if self._is_browser_or_vision_tool(item.get("tool_name", "")):
                        item["result"] = (
                            "[superseded_tool_result] This browser/vision frame was folded "
                            f"because a newer {tool_name} result follows in the same turn."
                        )
            pending_tool_results.append({
                "tool_use_id": tool_id,
                "tool_name": tool_name,
                "result": result,
            })

    @staticmethod
    def _is_browser_or_vision_tool(tool_name: str) -> bool:
        name = str(tool_name or "")
        return (
            name.startswith("browser_")
            or name.startswith("native_browser_")
            or name in {"capture_screenshot", "ocr_image", "vision_analyze"}
        )

    def _record_tool_call_history(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        result_views: Any,
        *,
        success: Optional[bool] = None,
        synthetic_tool_call: bool = False,
    ) -> None:
        history_entry = {
            "tool_name": tool_name,
            "input": redact_sensitive_data(tool_input),
            "result": result_views.log,
            "raw_ref": result_views.raw_ref,
            "truncated": result_views.truncated,
            "timestamp": time.time(),
        }
        if success is not None:
            history_entry["success"] = success
        if synthetic_tool_call:
            history_entry["synthetic_tool_call"] = True
        self._state.tool_call_history.append(history_entry)

    def _tool_failure_signature(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        try:
            normalized = json.dumps(tool_input or {}, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            normalized = str(tool_input)
        return f"{tool_name}:{normalized}"

    def _record_tool_failure_streak(self, signature: str, failed: bool) -> None:
        if failed:
            self._tool_failure_streak[signature] = self._tool_failure_streak.get(signature, 0) + 1
        else:
            self._tool_failure_streak.clear()

    def _tool_result_indicates_failure(self, text: str) -> bool:
        raw = (text or "").strip()
        if not raw:
            return False
        try:
            parsed = json.loads(raw)
        except Exception:
            return raw.startswith("[tool_error]") or '"ok": false' in raw.lower()
        return isinstance(parsed, dict) and parsed.get("ok") is False

    def _build_assistant_history_content(
        self,
        response: Any,
        current_text: str,
        tool_use_events: List[Dict[str, Any]],
    ) -> Any:
        # Provider protocol: assistant(tool_calls) must precede queued tool_result messages.
        if tool_use_events:
            text_part = ""
            if response is not None and hasattr(response, "content"):
                rc = response.content
                if isinstance(rc, dict):
                    text_part = rc.get("text", "") or current_text
                elif isinstance(rc, str):
                    text_part = rc or current_text
                else:
                    text_part = current_text
            else:
                text_part = current_text
            return {
                "text": text_part,
                "tool_calls": tool_use_events,
            }
        if response is not None and hasattr(response, "content"):
            return response.content
        if current_text:
            return current_text
        return None

    @staticmethod
    def _clean_textual_tool_call_response(
        final_response: str,
        current_text: str,
        visible_text: str,
        call_text: str,
    ) -> str:
        if call_text and call_text in final_response:
            return final_response.replace(call_text, "", 1).strip()
        if current_text and current_text in final_response:
            return final_response.replace(current_text, visible_text, 1).strip()
        return visible_text or final_response

    def _append_assistant_and_tool_results(
        self,
        assistant_content: Any,
        pending_tool_results: List[Dict[str, str]],
    ) -> None:
        if assistant_content:
            self._history.append_assistant(assistant_content)

        # Keep this order explicit: assistant(tool_call) must be stored before tool_result.
        for item in pending_tool_results:
            self._history.append_tool_result(
                item["tool_use_id"],
                item["result"],
                tool_name=item.get("tool_name", "tool"),
            )

    async def _finish_successful_chat_turn(
        self,
        user_input: str,
        final_response: str,
        user_seq: int,
        *,
        emit_finished: bool,
        consolidate_context: bool,
    ) -> str:
        logging.getLogger("pawmate").info("[Turn] engine chat finished")
        await self._emit_turn_completed(TurnCompletedContext(
            user_input=user_input,
            final_response=final_response,
            user_seq=user_seq,
            consolidate_context=consolidate_context,
        ))
        if emit_finished:
            event_bus.publish(FinishedEvent(turn_id=self._get_active_turn_id()))
        return final_response

    # ============================================================
    # 任务规划执行（多步复杂任务自动拆解）
    # ============================================================

    async def _execute_todo_steps(self, user_input: str, plan: "Plan") -> str:
        """逐步执行 todo：每个步骤单独进入一次 ReAct 循环。"""
        if not plan.tasks:
            return await self.chat(user_input, emit_finished=False)

        total_steps = len(plan.tasks)
        completed_count = 0
        step_outputs: List[str] = []

        # 显示任务计划
        self._format_workflow_todo(plan)

        for index, task in enumerate(plan.tasks, 1):
            if not self._state.is_running:
                _log = logging.getLogger("pawmate")
                _log.info("[Engine] TODO task interrupted at step %d/%d", index, total_steps)
                break
            task.status = "running"
            try:
                desc = task.description[:30]
                event_bus.publish(ProgressUpdateEvent(completed_count, total_steps, f"{task.id}: {desc}...", turn_id=self._get_active_turn_id()))
            except Exception:
                pass

            step_prompt = (
                f"{user_input}\n\n"
                f"你现在执行第 {index}/{total_steps} 步：[{task.id}] {task.description}\n"
                f"执行要求：\n"
                f"1. 只专注当前这一步，不要顺带执行后续步骤。\n"
                f"2. 必须调用真实工具完成动作类步骤，不要只给文字建议。\n"
                f"3. 完成后请用 '✓ [{task.id}]' 开头做简短结果汇报。\n"
                f"4. 请明确说明当前步骤的完成状态和输出结果。\n"
            )

            tool_count_before = len(self._state.tool_call_history)
            step_snapshot = self._history.snapshot()
            try:
                try:
                    step_response = await self.chat(
                        step_prompt,
                        emit_finished=False,
                        turn_id=self._get_active_turn_id(),
                    )
                except TypeError:
                    step_response = await self.chat(step_prompt, emit_finished=False)
            finally:
                self._history.rollback_to(step_snapshot)
            tool_count_after = len(self._state.tool_call_history)
            step_tool_count = tool_count_after - tool_count_before
            step_tool_entries = self._state.tool_call_history[tool_count_before:tool_count_after]
            task.result = step_response

            has_failure_text = self._is_failure_text(step_response)
            last_failure_reason = ""
            last_failure_index = -1
            last_success_index = -1
            for entry_index, entry in enumerate(step_tool_entries):
                failure_reason = self._step_tool_failure_reason(entry)
                if failure_reason:
                    last_failure_reason = failure_reason
                    last_failure_index = entry_index
                else:
                    last_success_index = entry_index

            unrecovered_tool_failure = bool(
                last_failure_reason and last_success_index <= last_failure_index
            )
            tool_failure_reason = last_failure_reason if unrecovered_tool_failure else ""

            completed = (not has_failure_text) and (step_tool_count >= 1) and not unrecovered_tool_failure

            if completed:
                task.status = "completed"
                completed_count += 1
                try:
                    event_bus.publish(ProgressUpdateEvent(completed_count, total_steps, f"完成: {task.id}", turn_id=self._get_active_turn_id()))
                except Exception:
                    pass
                step_outputs.append(step_response)
                try:
                    event_bus.publish(TextDeltaEvent(f"\n✅ 第 {index}/{total_steps} 步完成（调用 {step_tool_count} 个工具）：{task.description}\n", turn_id=self._get_active_turn_id()))
                except Exception:
                    pass
            else:
                task.status = "failed"
                if step_tool_count == 0 and not has_failure_text:
                    task.error = "步骤未调用任何工具，可能只产生文字而未真正执行"
                elif tool_failure_reason:
                    task.error = f"工具调用结果显示失败：{tool_failure_reason[:300]}"
                else:
                    task.error = step_response
                step_outputs.append(step_response)
                try:
                    event_bus.publish(TextDeltaEvent(f"\n❌ 第 {index}/{total_steps} 步失败：{task.description}\n", turn_id=self._get_active_turn_id()))
                except Exception:
                    pass

        summary_lines = ["\n执行摘要："]
        for idx, t in enumerate(plan.tasks, 1):
            icon = "✓" if t.status == "completed" else "✗"
            summary_lines.append(f"  {icon} {idx}. {t.id}: {t.description}")
            if t.error and t.status != "completed":
                summary_lines.append(f"     原因：{t.error[:100]}")

        return "\n\n".join(step_outputs + ["\n".join(summary_lines)])

    def _format_workflow_todo(self, plan: "Plan") -> None:
        lines = ["\n[TODO 工作流] 已拆解当前任务："]
        for idx, task in enumerate(plan.tasks, 1):
            lines.append(f"  {idx}. {task.description}")
        lines.append("[TODO 工作流] 正在执行中...\n")
        text = "\n".join(lines)
        try:
            event_bus.publish(TextDeltaEvent(text, turn_id=self._get_active_turn_id()))
        except Exception:
            pass

    def _step_tool_failure_reason(self, entry: Dict[str, Any]) -> str:
        result_text = str(entry.get("result", "") or "")
        result_lower = result_text.lower().strip()
        if entry.get("success") is False:
            return result_text or "工具调用失败"
        if (
            result_lower.startswith(("[tool_error]", "[error]", "[错误]"))
            or "[exit_code" in result_lower
            or "[approval_denied]" in result_lower
            or "[approval_timeout]" in result_lower
            or "[approval_unavailable]" in result_lower
            or "安全限制" in result_text
            or "已阻止该操作" in result_text
        ):
            return result_text
        return ""

    def _is_failure_text(self, text: str) -> bool:
        """
        改进的失败判定：要求关键词不被否定词修饰才算失败。
        """
        if not text:
            return False
        lowered = text.lower().strip()
        if lowered.startswith("[tool_error]") or lowered.startswith("[错误]"):
            return True
        if lowered.startswith("[error]"):
            return True

        FAIL_KW = ["失败", "无法完成", "未能", "执行出错"]
        NEG_PREFIX = ["没有", "无", "不存在", "未发现", "没出现", "没发生", "不是"]

        for line in lowered.splitlines():
            line = line.strip()
            for kw in FAIL_KW:
                if kw not in line:
                    continue
                idx = line.index(kw)
                prefix = line[max(0, idx - 10):idx]
                negated = any(neg in prefix for neg in NEG_PREFIX)
                if not negated:
                    return True
        return False

    async def shutdown(self) -> None:
        """释放引擎持有的外部资源（MCP 子进程、调度器等）。"""
        try:
            sched = getattr(self, "_scheduler", None)
            if sched is not None:
                sched.shutdown()
        except Exception:
            pass
        self.stop_heartbeat()
        try:
            await self._registry.shutdown()
        except Exception:
            pass

    def get_state(self) -> EngineState:
        """获取引擎当前状态"""
        return self._state

    # ============================================================
    # 桌宠心跳（委派到 HeartbeatController）
    # ============================================================

    def start_heartbeat(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """启动桌宠心跳任务，委派到 HeartbeatController。"""
        self._heartbeat.start(loop)
        self._worker_loop = loop or asyncio.get_event_loop()
        self._confirm_gate.set_worker_loop(self._worker_loop)

    def stop_heartbeat(self) -> None:
        """停止桌宠心跳任务，委派到 HeartbeatController。"""
        self._heartbeat.stop()

    def get_tool_call_history(self) -> List[Dict]:
        """获取工具调用历史"""
        return self._state.tool_call_history.copy()


# ── 模块级辅助 ──────────────────────────────────────────────

def _approval_llm_explain_enabled(app_config: Optional[dict]) -> bool:
    if not isinstance(app_config, dict):
        return True
    approval = app_config.get("approval") or {}
    if not isinstance(approval, dict):
        return True
    return approval.get("llm_explain_enabled", True) is not False


def _approval_auto_approve_enabled(app_config: Optional[dict]) -> bool:
    if not isinstance(app_config, dict):
        return False
    approval = app_config.get("approval") or {}
    if not isinstance(approval, dict):
        return False
    return approval.get("auto_approve_tools") is True


async def _explain_tool_approval(llm_client, tool_name: str, tool_input: dict) -> dict:
    safe_payload = json.dumps({
        "tool": tool_name,
        "input": tool_input,
    }, ensure_ascii=False, default=str)

    async def _collect() -> str:
        chunks: list[str] = []
        async for event in llm_client.stream(
            messages=[{
                "role": "user",
                "content": (
                    "请解释这次工具调用给用户审批。只返回 JSON，不要 Markdown。\n"
                    "字段：intent（一句话说明调用目的），plan（一句话说明会怎么执行），"
                    "risk（一句话说明用户需要注意的风险；低风险可写“无明显风险”）。\n"
                    "要求：中文，简短，人能看懂，不要泄露或复述已脱敏内容之外的秘密。\n\n"
                    f"工具调用：{safe_payload}"
                ),
            }],
            system="你是桌面助手的工具审批解释器。输出严格 JSON。",
            max_tokens=220,
        ):
            if event.get("type") == "text_delta":
                chunks.append(event.get("text", ""))
        return "".join(chunks).strip()

    raw = await asyncio.wait_for(_collect(), timeout=12.0)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {
                "intent": str(data.get("intent") or ""),
                "plan": str(data.get("plan") or ""),
                "risk": str(data.get("risk") or ""),
            }
    except Exception:
        pass
    return {"intent": raw, "plan": "", "risk": ""}


def _retire_legacy_markdown(store, memory) -> None:
    """一次性将 USER.md / MEMORY.md 退役为 legacy 备份，此后全走 SQLite。"""
    _log = logging.getLogger("pawmate")
    marker = store.get_meta("global", "legacy", "legacy_markdown_retired")
    if marker == "1":
        return

    data_dir = config.BASE_DIR / "data"
    for src_name, dst_name in [("USER.md", "user_legacy.md"), ("MEMORY.md", "memory_legacy.md")]:
        src = data_dir / src_name
        dst = data_dir / dst_name
        if src.exists() and not dst.exists():
            src.rename(dst)
            _log.info("[Legacy] %s -> %s", src_name, dst_name)

    store.set_meta("global", "legacy", "legacy_markdown_retired", "1")

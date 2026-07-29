"""
ReAct 引擎

实现标准的 Reasoning + Acting 循环，最多 10 轮
异步工厂模式初始化，自动加载 MCP 工具
"""
import asyncio
import hashlib
import json
import time
import logging
import re
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Dict, List, Optional
from dataclasses import dataclass

from pawmate.core.checkpoint.checkpoint import AgentCheckpoint, CheckpointStage, CheckpointStore
from pawmate.core.safety.confirm_gate import ConfirmGate
from pawmate.core.mcp_loader import load_mcp_tools
from pawmate.memory.long_term_memory_manager import LongTermMemoryManager
from pawmate.memory.memory_store import MemoryStore
from pawmate.context import ConversationContextManager
from pawmate.scheduling import init_scheduler
from pawmate.core.services.heartbeat import HeartbeatController
from pawmate.core.model.llm_factory import get_llm_client, get_llm_client_with_fallback, DEFAULT_SYSTEM_PROMPT
from pawmate.core.model.llm_provider import LLMProvider
from pawmate.core.model.llm_router import RouteChoice, classify_task_kind, resolve_llm_route_candidates
from pawmate.core.runtime.loop_control import CompletionStatus, LoopController, LoopDecision
from pawmate.core.model.provider_runner import ProviderRunner
from pawmate.core.planning.task_planner import TaskPlanner, Plan
from pawmate.core.tools.tool_router import route_tools_for_turn
from pawmate.tools.core.registry import ToolRegistry
from pawmate.core.prompts.prompt_assembler import build_runtime_prompt
from pawmate.core.tools.tool_parser import (
    is_truncated_tool_protocol,
    parse_textual_tool_call,
    strip_any_textual_tool_call_protocol,
)
from pawmate.core.tools.tool_call_runner import ToolCallRunner
from pawmate.core.tools.tool_call_lifecycle import (
    RuntimeToolCall,
    ToolCallStatus,
    reject_duplicate_tool_call_id,
)
from pawmate.core.tools.tool_observation import wrap_tool_observation_for_model
from pawmate.core.tools.tool_result_budget import budget_tool_result
from pawmate.core.observability.trace import AgentRunTrace
from pawmate.core.runtime.turn_context_builder import TurnContextBuilder
from pawmate.core.prompts.prompt_sections import (
    PromptSection,
    PromptSectionKind,
    TargetRole,
    TrustLevel,
    render_prompt_sections,
)
from pawmate.core.runtime.runtime_state import AgentRunState, ResourceLockManager, infer_tool_resources
from pawmate.tools.gateway.builtin_gateway import register_builtin_tools
from pawmate.tools.scheduler.tools import register_scheduler_tools
from pawmate.storage.history_store import HistoryStore
from pawmate.bridge.contracts import (
    ErrorEvent,
    FinishedEvent,
    ModelRuntimeEvent,
    ProgressUpdateEvent,
    ScheduledFiredEvent,
    TextDeltaEvent,
    ToolDoneEvent,
    ToolErrorEvent,
    ToolStartEvent,
)
from pawmate.bridge.event_bus import event_bus
from pawmate.core.safety.redaction import redact_for_json, redact_sensitive_data
from pawmate.core.safety.security_service import SecurityService
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
    end_seq: int | None = None


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
        self._uses_config_prompt = False
        self._active_route_choice: Optional[RouteChoice] = None
        self._state = EngineState(tool_call_history=[])
        self._run_state = AgentRunState()
        self._resource_locks = ResourceLockManager()
        self._trace = AgentRunTrace(self._run_state.run_id)
        self._checkpoint_store = CheckpointStore()
        self._active_chat_count = 0
        self._tool_failure_streak: Dict[str, int] = {}
        self._recent_task_route_tool_names: set[str] = set()
        self._nested_task_route_tool_names: set[str] = set()
        self._task_planner = TaskPlanner(llm_client=llm_client) if enable_task_planning else None

        # 错误处理和重试配置
        self._max_retries = 3
        self._retry_delay = 1.0  # 秒
        self._parallel_tool_limit = 4
        self._last_prompt_trace: Dict[str, Any] = {"turn_id": "", "sections": []}
        self._last_loop_decision: Dict[str, Any] = {}
        self._token_usage_total: Dict[str, Any] = self._empty_token_usage()
        self._token_usage_turn: Dict[str, Any] = self._empty_token_usage()
        self._tool_call_runner = ToolCallRunner(
            self._registry,
            max_retries=self._max_retries,
            retry_delay=self._retry_delay,
        )
        self._provider_runner = ProviderRunner(
            llm_client=self._llm,
            current_provider=current_provider,
        )
        self._provider_runner.set_status_callback(self._on_provider_runner_status)
        self._chat_lock: asyncio.Lock | None = None
        self._chat_owner_task: asyncio.Task | None = None
        self._turn_completed_tasks: set[asyncio.Task] = set()
        self._turn_completed_hooks: List[
            Callable[[TurnCompletedContext], Awaitable[None]]
        ] = []
        self._register_turn_completed_hook(self._record_successful_turn_on_completed)
        self._register_turn_completed_hook(self._consolidate_context_on_completed)
        self._turn_context_builder = TurnContextBuilder(
            static_prompt_provider=lambda: getattr(self, "_static_prompt", self._system_prompt),
            conversation_context_provider=lambda: self._conversation_context,
            long_term_memory_provider=lambda: self._long_term_memory,
            skill_context_provider=self._build_ready_skills_block,
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
        create_started = time.perf_counter()

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
        tools_started = time.perf_counter()
        register_builtin_tools(tool_registry, security_service=security_service)
        _log.info("[StartupTiming] builtin_tools %.2fs", time.perf_counter() - tools_started)

        # 加载 MCP server（委派到 mcp_loader）
        mcp_started = time.perf_counter()
        count = await load_mcp_tools(tool_registry)
        _log.info("[StartupTiming] mcp_tools %.2fs", time.perf_counter() - mcp_started)
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
        from pawmate.tools.memory.tools import register_memory_tools
        register_memory_tools(tool_registry, ltm)

        # 退役旧运行时记忆文件（USER.md / MEMORY.md → SQLite）
        _retire_legacy_markdown(store, ltm)

        # 定时任务系统初始化（委派到 scheduling 模块）
        scheduler = init_scheduler()
        if scheduler is not None:
            register_scheduler_tools(tool_registry, scheduler)

        # Prompt 组装（静态部分：身份+规则；动态记忆块每轮注入）
        prompt_started = time.perf_counter()
        static_prompt = build_runtime_prompt(
            tool_registry=tool_registry,
            core_mem=None,  # Dynamic memory is injected every turn by LongTermMemoryManager.
            system_prompt=system_prompt,
            app_config=app_config,
        )
        _log.info("[StartupTiming] static_prompt %.2fs", time.perf_counter() - prompt_started)

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
        engine._uses_config_prompt = system_prompt is None and app_config is not None
        engine._heartbeat.set_memory_context_provider(
            lambda: ltm.build_memory_block("心跳 闲置提醒 称呼 名字 PawMate 陪伴风格")
        )
        engine.apply_runtime_config(app_config)
        _log.info("[StartupTiming] agent_engine_create total %.2fs", time.perf_counter() - create_started)

        # ── 注册工具确认回调 ────────────────────────────
        engine._confirm_gate.setup()

        # ── 定时任务触发回调 ────────────────────────────────
        engine._scheduler = scheduler
        if scheduler is not None:
            scheduler.set_callback(engine._on_scheduled_fire)
            scheduler.start()

        # 注入 engine 引用到 ConfigBridge（供 settings 页查询记忆状态）
        try:
            from pawmate.bridge.config_bridge import inject_engine
            inject_engine(engine, bridge=None)
        except Exception:
            pass

        return engine

    async def _on_scheduled_fire(self, task_id: str, action: str) -> None:
        """Queue scheduled work behind an active chat instead of dropping it."""
        if self._state.is_running:
            logging.getLogger("pawmate").info(
                "[Scheduler] engine busy, queue task %s behind the active turn",
                task_id,
            )
        framed = (
            f"[Scheduled task fired: {task_id}]\n"
            f"Action to perform now: {action}\n\n"
            "Follow this instruction and briefly tell the user the result."
        )
        try:
            event_bus.publish(ScheduledFiredEvent(task_id, action))
        except Exception:
            pass
        try:
            # AgentEngine.chat owns the per-engine lock, so awaiting it creates
            # a bounded FIFO queue behind the current turn.
            await self.chat(framed, emit_finished=True)
        except Exception:
            logging.getLogger("pawmate").exception(
                "[Scheduler] task %s execution failed",
                task_id,
            )

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

    def get_runtime_state_snapshot(self) -> Dict[str, Any]:
        run_state = getattr(self, "_run_state", AgentRunState())
        resource_locks = getattr(self, "_resource_locks", None)
        engine_state = getattr(self, "_state", EngineState(tool_call_history=[]))
        try:
            active_session_id = self.get_active_session_id()
        except Exception:
            active_session_id = ""
        return {
            "run": run_state.to_dict(),
            "resources": resource_locks.snapshot() if resource_locks is not None else {"held": []},
            "trace": getattr(getattr(self, "_trace", None), "summary", lambda: {})(),
            "checkpoint": self.get_latest_checkpoint(),
            "engine": {
                "is_running": bool(getattr(engine_state, "is_running", False)),
                "tool_call_history_count": len(getattr(engine_state, "tool_call_history", []) or []),
                "active_session_id": active_session_id,
            },
            "loop": dict(getattr(self, "_last_loop_decision", {}) or {}),
        }

    def get_agent_run_trace(self) -> Dict[str, Any]:
        trace = getattr(self, "_trace", None)
        if trace is None:
            return {}
        return trace.to_dict()

    def export_agent_run_trace_json(self) -> str:
        trace = getattr(self, "_trace", None)
        if trace is None:
            return "{}"
        return trace.export_json()

    def get_latest_checkpoint(self) -> Dict[str, Any]:
        store = getattr(self, "_checkpoint_store", None)
        run_id = getattr(getattr(self, "_run_state", None), "run_id", "")
        if store is None or not run_id:
            return {}
        checkpoint = store.load_latest(run_id)
        return checkpoint.to_dict() if checkpoint is not None else {}

    def _save_checkpoint(
        self,
        stage: CheckpointStage | str,
        *,
        tool_call_id: str = "",
        tool_name: str = "",
        state: Optional[Dict[str, Any]] = None,
        side_effect: str = "",
        replay_allowed: bool = False,
    ) -> None:
        store = getattr(self, "_checkpoint_store", None)
        run_state = getattr(self, "_run_state", None)
        if store is None or run_state is None:
            return
        try:
            checkpoint = AgentCheckpoint.create(
                run_id=run_state.run_id,
                turn_id=str(self._get_active_turn_id()),
                stage=stage,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                state=state or {},
                side_effect=side_effect,
                idempotency_key=f"{run_state.run_id}:{self._get_active_turn_id()}:{tool_call_id}:{stage}",
                replay_allowed=replay_allowed,
            )
            store.save(checkpoint)
            self._record_trace_event(
                "checkpoint",
                tool_call_id=tool_call_id,
                data={
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "stage": checkpoint.stage.value,
                    "tool_name": tool_name,
                    "replay_allowed": checkpoint.replay_allowed,
                },
            )
        except Exception as exc:
            logging.getLogger("pawmate").warning("[Checkpoint] save failed: %s", exc)

    def _record_trace_event(
        self,
        event_type: str,
        *,
        tool_call_id: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        trace = getattr(self, "_trace", None)
        if trace is None:
            return
        trace.add_event(
            event_type,
            turn_id=str(self._get_active_turn_id()),
            tool_call_id=tool_call_id,
            data=data or {},
        )

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

    def apply_runtime_config(self, app_config: Optional[dict]) -> None:
        """Apply settings that affect live approval, shell policy, and prompting."""
        from pawmate.core.safety.shell_boundary import configure_shell_boundary

        self._app_config = app_config or {}
        self.apply_approval_config(self._app_config)
        security_cfg = self._app_config.get("security") or {}
        if not isinstance(security_cfg, dict):
            security_cfg = {}
        configure_shell_boundary(
            allow_browser_process_launch=security_cfg.get("allow_browser_process_launch") is True,
        )
        if self._uses_config_prompt:
            refreshed = build_runtime_prompt(
                tool_registry=self._registry,
                core_mem=None,
                app_config=self._app_config,
            )
            self._system_prompt = refreshed
            self._static_prompt = refreshed

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

    @staticmethod
    def _build_ready_skills_block() -> str:
        try:
            from pawmate.skills.runtime import build_ready_skills_block

            return build_ready_skills_block()
        except Exception:
            logging.getLogger("pawmate").exception("[Skills] failed to build runtime skill block")
            return ""

    def _compose_runtime_prompt(self, user_message: str = "") -> str:
        """Compose per-turn runtime system prompt from typed sections."""
        turn_id = str(self._get_active_turn_id())
        prompt = self._turn_context_builder.compose_runtime_prompt(user_message, turn_id=turn_id)
        trace = self._turn_context_builder.get_last_trace()
        route = getattr(self, "_active_route_choice", None)
        if route is None:
            self._last_prompt_trace = trace
            return prompt
        model = route.model or "(default)"
        route_section = PromptSection(
            name="RUNTIME_METADATA_LLM_ROUTE",
            kind=PromptSectionKind.RUNTIME_METADATA,
            trust=TrustLevel.TRUSTED_RUNTIME,
            content=(
                f"Current route: {route.kind} -> {route.provider}/{model}\n"
                "This describes provider routing only. It is not user intent, "
                "tool authorization, or a permission grant."
            ),
            source="llm_router",
            target_role=TargetRole.SYSTEM,
            priority=95,
            data_only=True,
        )
        trace["sections"].append(route_section.trace_entry())
        self._last_prompt_trace = trace
        return f"{prompt}\n\n{render_prompt_sections([route_section])}"

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
        self._active_route_choice = route
        self._sync_llm_from_provider_runner()

        logging.getLogger("pawmate").info(
            "[LLMRouter] %s -> %s/%s (%s)",
            route.kind,
            route.provider,
            route.model or "(default)",
            route.reason,
        )

    def _sync_llm_from_provider_runner(self) -> None:
        """Keep engine-side helper services on the active provider client."""
        self._llm = self._provider_runner.llm_client

        if self._task_planner is not None:
            self._task_planner.llm_client = self._llm

        context = getattr(self, "_conversation_context", None)
        attach_llm = getattr(context, "attach_llm", None)
        if callable(attach_llm):
            attach_llm(self._llm)

        heartbeat = getattr(self, "_heartbeat", None)
        set_llm_client = getattr(heartbeat, "set_llm_client", None)
        if callable(set_llm_client):
            set_llm_client(self._llm)
        self._publish_model_runtime("model_sync")

    @staticmethod
    def _empty_token_usage() -> Dict[str, Any]:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated": False,
        }

    @staticmethod
    def _coerce_token_count(value: Any) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, float):
            return max(0, int(value))
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
        return None

    def _normalize_token_usage(self, usage: Dict[str, Any] | None, *, estimated: bool = False) -> Dict[str, Any]:
        data = usage or {}
        prompt = self._coerce_token_count(data.get("prompt_tokens"))
        completion = self._coerce_token_count(data.get("completion_tokens"))
        total = self._coerce_token_count(data.get("total_tokens"))
        if total is None and (prompt is not None or completion is not None):
            total = int(prompt or 0) + int(completion or 0)
        return {
            "prompt_tokens": int(prompt or 0),
            "completion_tokens": int(completion or 0),
            "total_tokens": int(total or 0),
            "estimated": bool(data.get("estimated", estimated)),
        }

    def _has_token_usage(self, usage: Dict[str, Any] | None) -> bool:
        if not usage:
            return False
        normalized = self._normalize_token_usage(usage)
        return (
            normalized["prompt_tokens"] > 0
            or normalized["completion_tokens"] > 0
            or normalized["total_tokens"] > 0
        )

    @staticmethod
    def _estimate_token_count_from_text(text: str) -> int:
        if not text:
            return 0
        ascii_chars = 0
        other_chars = 0
        for ch in text:
            if ord(ch) < 128:
                ascii_chars += 1
            else:
                other_chars += 1
        estimated = (ascii_chars / 4.0) + other_chars
        return max(1, int(estimated + 0.999))

    def _estimate_model_usage(
        self,
        *,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools_defs: List[Dict[str, Any]],
        current_text: str,
    ) -> Dict[str, Any]:
        try:
            messages_text = json.dumps(messages, ensure_ascii=False, default=str)
        except Exception:
            messages_text = str(messages)
        try:
            tools_text = json.dumps(tools_defs, ensure_ascii=False, default=str)
        except Exception:
            tools_text = str(tools_defs)
        prompt_text = "\n".join([system_prompt or "", messages_text, tools_text])
        prompt_tokens = self._estimate_token_count_from_text(prompt_text)
        completion_tokens = self._estimate_token_count_from_text(current_text or "")
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "estimated": True,
        }

    def _record_token_usage(self, usage: Dict[str, Any] | None) -> Dict[str, Any]:
        normalized = self._normalize_token_usage(usage)
        for bucket in (self._token_usage_turn, self._token_usage_total):
            bucket["prompt_tokens"] = int(bucket.get("prompt_tokens", 0)) + normalized["prompt_tokens"]
            bucket["completion_tokens"] = int(bucket.get("completion_tokens", 0)) + normalized["completion_tokens"]
            bucket["total_tokens"] = int(bucket.get("total_tokens", 0)) + normalized["total_tokens"]
            bucket["estimated"] = bool(bucket.get("estimated", False) or normalized.get("estimated", False))
        return normalized

    def _model_runtime_payload(
        self,
        event: str,
        last_usage: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        provider_runner = getattr(self, "_provider_runner", None)
        provider = (
            getattr(provider_runner, "current_provider", None)
            or getattr(self._llm, "_provider", None)
            or ""
        )
        model = (
            getattr(provider_runner, "current_model", None)
            or getattr(self._llm, "_model", None)
            or getattr(self._llm, "model", None)
            or ""
        )
        route = getattr(self, "_active_route_choice", None)
        return {
            "event": event,
            "turn_id": self._get_active_turn_id(),
            "provider": provider,
            "model": model,
            "route_kind": getattr(route, "kind", "") if route is not None else "",
            "usage": {
                "last": self._normalize_token_usage(last_usage) if last_usage else self._empty_token_usage(),
                "turn": dict(self._token_usage_turn),
                "total": dict(self._token_usage_total),
            },
        }

    def _publish_model_runtime(self, event: str, last_usage: Dict[str, Any] | None = None) -> None:
        try:
            payload = self._model_runtime_payload(event, last_usage)
            event_bus.publish(ModelRuntimeEvent(json.dumps(payload, ensure_ascii=False, default=str)))
        except Exception:
            logging.getLogger("pawmate").debug("[ModelRuntime] publish failed", exc_info=True)

    def publish_model_runtime_status(self, event: str = "engine_ready") -> None:
        self._publish_model_runtime(event)

    def _on_provider_runner_status(self, provider_event: Dict[str, Any]) -> None:
        try:
            event_name = str(provider_event.get("event") or "provider_status")
            if event_name == "provider_fallback_switch":
                self._sync_llm_from_provider_runner()
            payload = self._model_runtime_payload(event_name)
            provider = str(provider_event.get("provider") or "").strip()
            model = str(provider_event.get("model") or "").strip()
            if provider:
                payload["provider"] = provider
            if model:
                payload["model"] = model
            payload["provider_event"] = dict(provider_event)
            event_bus.publish(ModelRuntimeEvent(json.dumps(payload, ensure_ascii=False, default=str)))
        except Exception:
            logging.getLogger("pawmate").debug("[ModelRuntime] provider status publish failed", exc_info=True)

    def _register_turn_completed_hook(
        self,
        hook: Callable[[TurnCompletedContext], Awaitable[None]],
    ) -> None:
        self._turn_completed_hooks.append(hook)

    async def _emit_turn_completed(self, context: TurnCompletedContext) -> None:
        for hook in list(self._turn_completed_hooks):
            await hook(context)

    def _schedule_turn_completed_hooks(self, context: TurnCompletedContext) -> None:
        task = asyncio.create_task(self._emit_turn_completed_safely(context))
        self._turn_completed_tasks.add(task)

        def _forget(done_task: asyncio.Task) -> None:
            self._turn_completed_tasks.discard(done_task)

        task.add_done_callback(_forget)

    async def _emit_turn_completed_safely(self, context: TurnCompletedContext) -> None:
        try:
            await self._emit_turn_completed(context)
        except Exception as exc:
            logging.getLogger("pawmate").warning(
                "[Turn] background completion hooks failed: %s",
                exc,
                exc_info=True,
            )

    async def _record_successful_turn_on_completed(
        self,
        context: TurnCompletedContext,
    ) -> None:
        await self._record_successful_turn(
            context.user_input,
            context.final_response,
            context.user_seq,
            context.end_seq,
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
        end_seq: int | None = None,
    ) -> None:
        manager = getattr(self, "_long_term_memory", None)
        callback = getattr(manager, "on_successful_turn_end", None)
        if callback is None:
            return

        if end_seq is None:
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

    def _get_task_timeout_seconds(self) -> float:
        """Return the configured turn deadline; zero explicitly disables it."""
        runtime_cfg = self._app_config.get("runtime", {}) if isinstance(self._app_config, dict) else {}
        value = runtime_cfg.get("task_timeout_seconds") if isinstance(runtime_cfg, dict) else None
        if value is None:
            try:
                cfg = json.loads((config.BASE_DIR / "config.json").read_text("utf-8"))
                value = cfg.get("runtime", {}).get("task_timeout_seconds", 0)
            except Exception:
                value = 0
        try:
            return max(0.0, float(value or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _browser_process_launch_enabled(self) -> bool:
        security_cfg = self._app_config.get("security", {}) if isinstance(self._app_config, dict) else {}
        return isinstance(security_cfg, dict) and security_cfg.get("allow_browser_process_launch") is True

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

    def _build_followup_tool_route_context(self, user_input: str) -> str:
        """Build a compact route hint for short follow-up turns."""
        context_parts: list[str] = []
        try:
            recent_messages = self._history.get_messages_windowed(max_chars=12000)[-12:]
        except Exception:
            recent_messages = []
        current = (user_input or "").strip()
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
            content_text = content_text.strip()
            if not content_text or (role == "user" and content_text == current):
                continue
            context_parts.append(f"{role}: {content_text[:1200]}")

        context_blob = "\n".join(context_parts)
        compact = re.sub(r"\s+", "", current.lower())
        commit_followup = compact in {"好", "好的", "可以", "行", "没问题", "动手", "整吧", "整理吧"}
        inspect_followup = (
            compact in {"再看看", "仔细看看", "再仔细看看"}
            or bool(re.search(r"(再|重新|继续).{0,6}(看|扫|检查)", current.lower()))
        )
        if commit_followup and re.search(r"(整理|分类|归类|移动|动手|方案|收拾|归档)", context_blob):
            return f"整理 移动 文件\n{current}"
        if inspect_followup and re.search(r"(桌面|文件|清单|快捷方式|文件夹|desktop)", context_blob, re.IGNORECASE):
            return f"桌面内容 有哪些文件\n{current}"
        return "\n".join([current, context_blob]).strip()

    @staticmethod
    def _looks_like_contextual_followup(user_input: str) -> bool:
        text = str(user_input or "").strip().lower()
        if not text:
            return False
        compact = re.sub(r"\s+", "", text)
        if len(compact) <= 12 and compact in {
            "好",
            "好的",
            "可以",
            "行",
            "没问题",
            "继续",
            "动手",
            "整吧",
            "整理吧",
            "再看看",
            "仔细看看",
            "再仔细看看",
        }:
            return True
        return bool(
            re.search(
                r"(再|重新|继续|接着|换个方式).{0,16}"
                r"(试|打开|看|扫|检查|搜索|操作|整理|执行|动手|访问)",
                text,
            )
            or (
                re.search(r"(桌面版|移动版|网页版)", text)
                and re.search(r"(再|重新|继续|重试|试一次|换个)", text)
            )
        )

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
        current_task = asyncio.current_task()
        if self._chat_owner_task is current_task:
            return await self._chat_unlocked(user_input, emit_finished=emit_finished, turn_id=turn_id)
        lock = self._get_chat_lock()
        async with lock:
            self._chat_owner_task = current_task
            self._running_turn_id = int(turn_id or 0)
            try:
                return await self._chat_unlocked(user_input, emit_finished=emit_finished, turn_id=turn_id)
            finally:
                self._chat_owner_task = None
                self._running_turn_id = 0

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
            self._run_state = AgentRunState()
            self._trace = AgentRunTrace(self._run_state.run_id)
            self._active_chat_count += 1
            self._state.is_running = True
            self._tool_failure_streak.clear()
            active_turn_id = int(turn_id or int(time.time() * 1000))
            turn_token = _ACTIVE_TURN_ID.set(active_turn_id)
            self._run_state.start_turn(str(active_turn_id))
            self._save_checkpoint(
                CheckpointStage.TURN_STARTED,
                state={"user_input": user_input},
            )
            self._record_trace_event(
                "turn_started",
                data={"user_input": user_input},
            )
            self._token_usage_turn = self._empty_token_usage()
            self._publish_model_runtime("turn_started")
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
            if entering_depth == 0 and self._run_state.current_turn is not None:
                self._run_state.current_turn.user_seq = user_seq
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
            seen_tool_call_ids: set[str] = set()
            truncation_retries_left = 2
            requires_tool_evidence = False
            successful_tool_evidence = False
            web_evidence_sources: List[Dict[str, str]] = []
            loop_controller = LoopController(
                max_iterations=self._get_max_turns(),
                max_tool_calls=max_tool_calls,
                max_runtime_seconds=self._get_task_timeout_seconds(),
            )
            self._last_loop_decision = loop_controller.snapshot()
            loop_stop_decision: LoopDecision | None = None
            while True:
                iteration_decision = loop_controller.before_iteration(tool_calls_used)
                self._last_loop_decision = iteration_decision.to_dict()
                if not iteration_decision.should_continue:
                    loop_stop_decision = iteration_decision
                    self._record_trace_event("loop_decision", data=loop_stop_decision.to_dict())
                    final_response = self._append_loop_stop_response(final_response, loop_stop_decision)
                    break
                turn_count = iteration_decision.iteration
                self._sync_memory_conversation_source(
                    start_seq=user_seq,
                    end_seq=self._history.snapshot(),
                )

                # 调 LLM，获取响应（带上下文窗口限制）
                current_text = ""
                had_tool_use = False
                tool_use_events: List[Dict[str, Any]] = []
                tool_call_requests: List[RuntimeToolCall] = []
                pending_tool_results: List[Dict[str, str]] = []
                remaining_tool_calls = max(0, max_tool_calls - tool_calls_used)
                tool_safety_limit_reached = remaining_tool_calls <= 0
                route_context = self._build_tool_route_context(user_input)
                if tool_safety_limit_reached:
                    tool_route = None
                    tools_defs = []
                else:
                    tool_route = route_tools_for_turn(
                        user_input,
                        (tool["name"] for tool in self._registry.list_tools()),
                        sticky_tools=task_route_tool_names or None,
                        allow_browser_process_launch=self._browser_process_launch_enabled(),
                    )
                    if tool_route.kind == "chat" and self._looks_like_contextual_followup(user_input):
                        context_route_input = self._build_followup_tool_route_context(user_input)
                        context_route = route_tools_for_turn(
                            context_route_input,
                            (tool["name"] for tool in self._registry.list_tools()),
                            sticky_tools=task_route_tool_names or None,
                            allow_browser_process_launch=self._browser_process_launch_enabled(),
                        )
                        if context_route.kind != "chat":
                            _log.info(
                                "[ToolRoute] contextual follow-up promoted %s -> %s",
                                tool_route.kind,
                                context_route.kind,
                            )
                            tool_route = context_route
                    # Keep sticky context focused on the diagnostic match, not
                    # on the complete registry exposed through tool_names.
                    task_route_tool_names = (
                        set(tool_route.matched_tool_names)
                        | set(tool_route.inherited_tool_names)
                    )
                    if tool_route.kind != "chat":
                        requires_tool_evidence = True
                    if tool_route.tool_names:
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
                self._record_trace_event(
                    "model_request",
                    data={
                        "provider": self._provider_runner.current_provider,
                        "model": self._provider_runner.current_model,
                        "message_count": len(messages),
                        "tool_names": [str(tool.get("name") or "") for tool in tools_defs],
                        "system_chars": len(system_prompt or ""),
                        "messages_budget": messages_budget,
                        "context_sections": self._last_prompt_trace.get("sections", []),
                    },
                )

                final_response_before_stream = final_response
                try:
                    current_text, final_response, stream_events = await self._provider_runner.collect_stream_events(
                        messages=messages,
                        tools_defs=tools_defs,
                        protocol_filter_tools_defs=tools_defs,
                        final_response=final_response,
                        system_prompt_factory=lambda prompt=system_prompt: prompt,
                        turn_id=self._get_active_turn_id(),
                        max_tokens=8192,
                    )
                finally:
                    self._sync_llm_from_provider_runner()
                response_for_history = self._provider_runner.get_last_response()
                token_usage = self._provider_runner.get_last_usage()
                if not self._has_token_usage(token_usage):
                    token_usage = self._estimate_model_usage(
                        system_prompt=system_prompt,
                        messages=messages,
                        tools_defs=tools_defs,
                        current_text=current_text,
                    )
                recorded_token_usage = self._record_token_usage(token_usage)
                self._record_trace_event(
                    "model_response",
                    data={
                        "provider": self._provider_runner.current_provider,
                        "model": self._provider_runner.current_model,
                        "text_chars": len(current_text or ""),
                        "stream_event_types": [event.get("type", "") for event in stream_events],
                        "provider_attempt_trace": self._provider_runner.get_last_attempt_trace(),
                        "token_usage": recorded_token_usage,
                    },
                )

                # 处理收集到的流式事件
                self._publish_model_runtime("model_response", recorded_token_usage)
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
                        requires_tool_evidence = True
                        runtime_tool_call = self._build_runtime_tool_call_request(
                            event,
                            seen_tool_call_ids,
                        )
                        task_route_tool_names.add(runtime_tool_call.name)

                        tool_use_events.append(runtime_tool_call.to_assistant_tool_call())
                        if tool_route is None or runtime_tool_call.name not in tool_route.tool_names:
                            runtime_tool_call.result_success = False
                            try:
                                runtime_tool_call.transition(ToolCallStatus.FAILED, reason="route_denied")
                            except Exception:
                                pass
                            denied_message = (
                                "[tool_error] Tool call denied by runtime route hard gate: "
                                f"{runtime_tool_call.name}"
                            )
                            event_bus.publish(ToolErrorEvent(
                                runtime_tool_call.name,
                                self._build_tool_card_payload(
                                    runtime_tool_call.name,
                                    runtime_tool_call.arguments,
                                    stage="error",
                                    result=denied_message,
                                    success=False,
                                ),
                                turn_id=self._get_active_turn_id(),
                            ))
                            logging.getLogger("pawmate.operation").warning(
                                "[Tool] route_denied name=%s route=%s",
                                runtime_tool_call.name,
                                getattr(tool_route, "kind", "none"),
                            )
                            _log.warning(
                                "[ToolRoute] denied provider tool_use name=%s route=%s",
                                runtime_tool_call.name,
                                getattr(tool_route, "kind", "none"),
                            )
                            pending_tool_results.append({
                                "tool_use_id": runtime_tool_call.id,
                                "tool_name": runtime_tool_call.name,
                                "result": wrap_tool_observation_for_model(
                                    tool_call_id=runtime_tool_call.id,
                                    tool_name=runtime_tool_call.name,
                                    content=denied_message,
                                    success=False,
                                    redacted=True,
                                ),
                            })
                            continue
                        tool_call_requests.append(runtime_tool_call)

                if dropped_tool_calls:
                    _log.warning(
                        "[ToolBudget] dropped %d tool call(s) after per-turn limit %d",
                        dropped_tool_calls,
                        max_tool_calls,
                    )

                if tool_call_requests:
                    await self._handle_tool_call_batch(tool_call_requests, pending_tool_results)
                    tool_calls_used += len(tool_call_requests)
                    for call in tool_call_requests:
                        observation_fingerprint = self._pending_tool_result_fingerprint(
                            pending_tool_results,
                            call.id,
                        )
                        if call.result_success is True:
                            successful_tool_evidence = True
                        if call.name == "native_web_search" and call.result_success is True:
                            web_evidence_sources.extend(
                                self._pending_web_evidence_sources(pending_tool_results, call.id)
                            )
                        loop_decision = loop_controller.record_tool_call(
                            call.name,
                            call.arguments,
                            success=call.result_success is not False,
                            observation_fingerprint=observation_fingerprint,
                        )
                        self._last_loop_decision = loop_decision.to_dict()
                        if not loop_decision.should_continue and loop_stop_decision is None:
                            loop_stop_decision = loop_decision

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
                        requires_tool_evidence = True
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
                        runtime_tool_call = self._build_runtime_tool_call_request(
                            {
                                "id": tool_id,
                                "name": tool_name,
                                "input": tool_input,
                                "provider": "textual_protocol",
                            },
                            seen_tool_call_ids,
                        )
                        task_route_tool_names.add(runtime_tool_call.name)

                        tool_use_events.append(runtime_tool_call.to_assistant_tool_call())

                        async with self._resource_locks.hold_many(
                            infer_tool_resources(tool_name, tool_input),
                            owner=tool_id,
                        ):
                            tool_result_ui = await self._handle_tool_call_event(
                                tool_name,
                                tool_input,
                                tool_id,
                                pending_tool_results,
                                synthetic_tool_call=True,
                                runtime_tool_call=runtime_tool_call,
                            )
                        if tool_result_ui is not None:
                            if final_response:
                                final_response = f"{final_response}\n\n[工具结果] {tool_result_ui}"
                            else:
                                final_response = tool_result_ui
                        tool_calls_used += 1
                        observation_fingerprint = self._pending_tool_result_fingerprint(
                            pending_tool_results,
                            runtime_tool_call.id,
                        )
                        if runtime_tool_call.result_success is True:
                            successful_tool_evidence = True
                        if tool_name == "native_web_search" and runtime_tool_call.result_success is True:
                            web_evidence_sources.extend(
                                self._pending_web_evidence_sources(
                                    pending_tool_results,
                                    runtime_tool_call.id,
                                )
                            )
                        loop_decision = loop_controller.record_tool_call(
                            tool_name,
                            tool_input,
                            success=runtime_tool_call.result_success is not False,
                            observation_fingerprint=observation_fingerprint,
                        )
                        self._last_loop_decision = loop_decision.to_dict()
                        if not loop_decision.should_continue and loop_stop_decision is None:
                            loop_stop_decision = loop_decision
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
                    completion_decision = loop_controller.complete(
                        CompletionStatus.COMPLETED,
                        "assistant_answered_without_tool_call",
                    )
                    self._last_loop_decision = completion_decision.to_dict()
                    self._record_trace_event("loop_decision", data=completion_decision.to_dict())
                    break

                if loop_stop_decision is not None:
                    self._record_trace_event("loop_decision", data=loop_stop_decision.to_dict())
                    final_response = self._append_loop_stop_response(final_response, loop_stop_decision)
                    break

            # 3. 完成
            if task_route_tool_names:
                if entering_depth == 0:
                    self._remember_recent_task_route(task_route_tool_names)
                else:
                    self._nested_task_route_tool_names.update(task_route_tool_names)
            if requires_tool_evidence and not successful_tool_evidence:
                final_response = self._build_unverified_tool_response(loop_stop_decision)
            elif web_evidence_sources:
                final_response = self._append_web_evidence_sources(
                    final_response,
                    web_evidence_sources,
                )
            return await self._finish_successful_chat_turn(
                user_input,
                final_response,
                user_seq,
                emit_finished=emit_finished,
                consolidate_context=True,
            )

        except asyncio.CancelledError:
            logging.getLogger("pawmate").info("[Turn] engine chat cancelled")
            if entering_depth == 0:
                self._run_state.finish_turn("cancelled", "cancelled")
                self._save_checkpoint(
                    CheckpointStage.TURN_FAILED,
                    state={"status": "cancelled", "error": "cancelled"},
                )
                self._record_trace_event(
                    "final_status",
                    data={"status": "cancelled", "error": "cancelled"},
                )
            raise

        except Exception as e:
            logging.getLogger("pawmate").error("[Turn] engine chat error: %s", e)
            if entering_depth == 0:
                self._run_state.finish_turn("failed", str(e))
                self._save_checkpoint(
                    CheckpointStage.PROVIDER_ERROR,
                    state={"error_type": type(e).__name__, "error": str(e)},
                )
                self._save_checkpoint(
                    CheckpointStage.TURN_FAILED,
                    state={"status": "failed", "error": str(e)},
                )
                self._record_trace_event(
                    "error",
                    data={"error_type": type(e).__name__, "error": str(e)},
                )
                self._record_trace_event(
                    "final_status",
                    data={"status": "failed", "error": str(e)},
                )
            error_msg = f"引擎错误: {str(e)}"
            event_bus.publish(ErrorEvent(error_msg, turn_id=self._get_active_turn_id()))
            raise

        finally:
            if entering_depth == 0 and self._run_state.last_status == "running":
                self._run_state.finish_turn("succeeded")
            _CHAT_DEPTH.reset(depth_token)
            if turn_token is not None:
                _ACTIVE_TURN_ID.reset(turn_token)
            if entering_depth == 0:
                self._active_chat_count = max(0, self._active_chat_count - 1)
                self._state.is_running = self._active_chat_count > 0

    def cancel_current_turn(self, turn_id: int = 0) -> int:
        """
        中断当前任务，不删除历史。

        旧逻辑会 rollback 到 turn 开始前，取消一次任务就可能把当前会话里
        已经展示/写入的消息删掉。取消应当是运行时控制，不应当是破坏性撤销。
        返回值保留给 UI 兼容，固定为 0（删除消息数）。
        """
        _log = logging.getLogger("pawmate")

        # 防御：如果 already not running，no-op
        requested_turn_id = int(turn_id or 0)
        running_turn_id = int(getattr(self, "_running_turn_id", 0) or 0)
        if requested_turn_id and requested_turn_id != running_turn_id:
            _log.info(
                "[Engine] ignored cancellation for non-running turn requested=%s running=%s",
                requested_turn_id,
                running_turn_id,
            )
            return 0

        if not self._state.is_running:
            _log.warning("[Engine] cancel_current_turn called but not running — no-op")
            return 0

        self._turn_snapshot_seq = None
        self._state.is_running = False

        # 取消正在等待的工具确认（委派给 ConfirmGate，无待确认时安全 no-op）
        self._confirm_gate.resolve(False)

        owner = getattr(self, "_chat_owner_task", None)
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if owner is not None and owner is not current and not owner.done():
            owner.cancel()
            _log.info("[Engine] requested cancellation of active chat task")

        _log.info("[Engine] cancelled without deleting history")
        return 0

    async def _handle_tool_call_batch(
        self,
        tool_calls: List[RuntimeToolCall],
        pending_tool_results: List[Dict[str, str]],
    ) -> None:
        if not tool_calls:
            return
        if len(tool_calls) <= 1 or self._parallel_tool_limit <= 1:
            for call in tool_calls:
                async with self._resource_locks.hold_many(
                    infer_tool_resources(call.name, call.arguments),
                    owner=call.id,
                ):
                    await self._handle_tool_call_event(
                        call.name,
                        call.arguments,
                        call.id,
                        pending_tool_results,
                        log_gateway_start=True,
                        runtime_tool_call=call,
                    )
            return

        semaphore = asyncio.Semaphore(self._parallel_tool_limit)

        async def run_one(call: RuntimeToolCall) -> List[Dict[str, str]]:
            local_pending: List[Dict[str, str]] = []
            async with semaphore:
                async with self._resource_locks.hold_many(
                    infer_tool_resources(call.name, call.arguments),
                    owner=call.id,
                ):
                    await self._handle_tool_call_event(
                        call.name,
                        call.arguments,
                        call.id,
                        local_pending,
                        log_gateway_start=True,
                        runtime_tool_call=call,
                    )
            return local_pending

        batches = await asyncio.gather(*(run_one(call) for call in tool_calls))
        for local_pending in batches:
            pending_tool_results.extend(local_pending)

    def _build_runtime_tool_call_request(
        self,
        event: Dict[str, Any],
        seen_tool_call_ids: set[str],
    ) -> RuntimeToolCall:
        runtime_tool_call = RuntimeToolCall.from_provider_event(
            event,
            run_id=getattr(getattr(self, "_run_state", None), "run_id", ""),
            turn_id=str(self._get_active_turn_id()),
        )
        reject_duplicate_tool_call_id(runtime_tool_call.id, seen_tool_call_ids)
        self._record_trace_event(
            "tool_call_parsed",
            tool_call_id=runtime_tool_call.id,
            data={
                "tool_name": runtime_tool_call.name,
                "source_provider": runtime_tool_call.source_provider,
                "lifecycle_status": runtime_tool_call.status.value,
                "validation_result": runtime_tool_call.validation_result,
            },
        )
        return runtime_tool_call

    async def _handle_tool_call_event(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        tool_id: str,
        pending_tool_results: List[Dict[str, str]],
        *,
        synthetic_tool_call: bool = False,
        log_gateway_start: bool = False,
        runtime_tool_call: RuntimeToolCall | None = None,
    ) -> Optional[str]:
        _log = logging.getLogger("pawmate")
        operation_log = logging.getLogger("pawmate.operation")
        tool_def = self._registry.get_tool(tool_name) if hasattr(self._registry, "get_tool") else None
        tool_tags = set(getattr(tool_def, "tags", []) or [])
        hide_tool_card = bool(tool_tags & {"data_only", "internal", "hidden"})
        if not hide_tool_card:
            event_bus.publish(ToolStartEvent(
                tool_name,
                self._build_tool_card_payload(tool_name, tool_input, stage="start"),
                turn_id=self._get_active_turn_id(),
            ))
        operation_log.info(
            "[Tool] start name=%s hidden_ui=%s synthetic=%s",
            tool_name,
            hide_tool_card,
            synthetic_tool_call,
        )
        if log_gateway_start:
            _log.info("[GATEWAY] 调用工具: %s", tool_name)
        self._mark_runtime_tool_call_executing(runtime_tool_call)
        self._record_trace_event(
            "tool_execution_started",
            tool_call_id=tool_id,
            data={
                "tool_name": tool_name,
                "input": tool_input,
                "synthetic_tool_call": synthetic_tool_call,
            },
        )
        side_effect = str(getattr(tool_def, "side_effect", "") or "")
        if getattr(tool_def, "approval", "") == "confirm":
            self._save_checkpoint(
                CheckpointStage.CONFIRM_PENDING,
                tool_call_id=tool_id,
                tool_name=tool_name,
                side_effect=side_effect,
                state={
                    "tool_input": tool_input,
                    "approval": "confirm",
                },
            )
        self._save_checkpoint(
            CheckpointStage.BEFORE_TOOL_EXECUTION,
            tool_call_id=tool_id,
            tool_name=tool_name,
            side_effect=side_effect,
            state={
                "tool_input": tool_input,
                "synthetic_tool_call": synthetic_tool_call,
                "lifecycle_status": runtime_tool_call.status.value if runtime_tool_call else "",
            },
        )

        failure_signature = self._tool_failure_signature(tool_name, tool_input)
        if self._tool_failure_streak.get(failure_signature, 0) >= 3:
            message = (
                f"[tool_repeated_failure] {tool_name} with the same key arguments has failed 3 times. "
                "Stop retrying this exact call; choose another tool, wait for a different condition, "
                "use returned candidates, or ask the user for clarification."
            )
            views = budget_tool_result(tool_name, message, session_id=self.get_active_session_id())
            if not hide_tool_card:
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
            operation_log.warning("[Tool] repeated_failure name=%s", tool_name)
            self._queue_pending_tool_result(pending_tool_results, tool_id, tool_name, views.model)
            self._finish_runtime_tool_call(runtime_tool_call, success=False)
            self._save_checkpoint(
                CheckpointStage.AFTER_TOOL_EXECUTION,
                tool_call_id=tool_id,
                tool_name=tool_name,
                side_effect=side_effect,
                state={
                    "success": False,
                    "error": "repeated_failure",
                    "result_truncated": views.truncated,
                    "raw_ref": views.raw_ref,
                },
            )
            self._record_tool_trace_finished(
                tool_id,
                tool_name,
                views,
                success=False,
                error="repeated_failure",
            )
            self._record_tool_call_history(
                tool_name,
                tool_input,
                views,
                success=False,
                synthetic_tool_call=synthetic_tool_call,
                runtime_tool_call=runtime_tool_call,
            )
            return None

        outcome = await self._tool_call_runner.run(
            tool_name,
            tool_input,
            session_id=self.get_active_session_id(),
        )
        self._record_policy_and_confirm_trace(
            tool_id,
            tool_name,
            policy_decision=outcome.policy_decision,
            approval_decision=outcome.approval_decision,
        )
        tool_result_failed = (not outcome.success) or self._tool_result_indicates_failure(outcome.views.model)
        self._record_tool_failure_streak(failure_signature, tool_result_failed)
        if outcome.success and not tool_result_failed:
            if log_gateway_start:
                _log.info("[GATEWAY] %s completed (%.1fs)", tool_name, outcome.elapsed)

            if not hide_tool_card:
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
            operation_log.info(
                "[Tool] done name=%s elapsed=%.1fs truncated=%s raw_ref=%s",
                tool_name,
                outcome.elapsed,
                outcome.views.truncated,
                outcome.views.raw_ref or "",
            )

            self._queue_pending_tool_result(
                pending_tool_results,
                tool_id,
                tool_name,
                outcome.views.model,
            )
            self._finish_runtime_tool_call(runtime_tool_call, success=True)
            self._save_checkpoint(
                CheckpointStage.AFTER_TOOL_EXECUTION,
                tool_call_id=tool_id,
                tool_name=tool_name,
                side_effect=side_effect,
                state={
                    "success": True,
                    "elapsed": outcome.elapsed,
                    "result_truncated": outcome.views.truncated,
                    "raw_ref": outcome.views.raw_ref,
                },
            )
            self._record_tool_trace_finished(
                tool_id,
                tool_name,
                outcome.views,
                success=True,
                elapsed=outcome.elapsed,
            )
            self._record_tool_call_history(
                tool_name,
                tool_input,
                outcome.views,
                synthetic_tool_call=synthetic_tool_call,
                runtime_tool_call=runtime_tool_call,
            )
            return outcome.views.ui

        _log.error("[GATEWAY] %s failed: %s", tool_name, outcome.error)
        if not hide_tool_card:
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
        operation_log.warning(
            "[Tool] failed name=%s elapsed=%.1fs error=%s",
            tool_name,
            outcome.elapsed,
            outcome.error,
        )

        self._queue_pending_tool_result(
            pending_tool_results,
            tool_id,
            tool_name,
            outcome.views.model,
        )
        self._finish_runtime_tool_call(runtime_tool_call, success=False)
        self._save_checkpoint(
            CheckpointStage.AFTER_TOOL_EXECUTION,
            tool_call_id=tool_id,
            tool_name=tool_name,
            side_effect=side_effect,
            state={
                "success": False,
                "elapsed": outcome.elapsed,
                "error": str(outcome.error or ""),
                "result_truncated": outcome.views.truncated,
                "raw_ref": outcome.views.raw_ref,
            },
        )
        self._record_tool_trace_finished(
            tool_id,
            tool_name,
            outcome.views,
            success=False,
            elapsed=outcome.elapsed,
            error=str(outcome.error or ""),
        )
        self._record_tool_call_history(
            tool_name,
            tool_input,
            outcome.views,
            success=False,
            synthetic_tool_call=synthetic_tool_call,
            runtime_tool_call=runtime_tool_call,
        )
        return None

    def _record_policy_and_confirm_trace(
        self,
        tool_id: str,
        tool_name: str,
        *,
        policy_decision=None,
        approval_decision=None,
    ) -> None:
        tool_def = self._registry.get_tool(tool_name) if hasattr(self._registry, "get_tool") else None
        if policy_decision is None:
            policy_decision = getattr(self._registry, "_last_policy_decision", None)
        if tool_def is not None and policy_decision is not None:
            self._record_trace_event(
                "policy_decision",
                tool_call_id=tool_id,
                data={
                    "tool_name": tool_name,
                    "allowed": bool(getattr(policy_decision, "allowed", False)),
                    "reason": str(getattr(policy_decision, "reason", "")),
                    "requires_confirm": bool(getattr(policy_decision, "requires_confirm", False)),
                    "risk": str(getattr(policy_decision, "risk", "")),
                    "side_effect": str(getattr(policy_decision, "side_effect", "")),
                },
            )
        if getattr(tool_def, "approval", "") != "confirm":
            return
        approval = approval_decision
        if approval is None:
            approval = getattr(self._registry, "_last_approval", None)
        if approval is None:
            return
        self._record_trace_event(
            "confirm_decision",
            tool_call_id=tool_id,
            data={
                "tool_name": tool_name,
                "allowed": bool(getattr(approval, "allowed", False)),
                "reason": str(getattr(approval, "reason", "")),
            },
        )

    def _record_tool_trace_finished(
        self,
        tool_id: str,
        tool_name: str,
        result_views: Any,
        *,
        success: bool,
        elapsed: float | None = None,
        error: str = "",
    ) -> None:
        self._record_trace_event(
            "tool_execution_finished",
            tool_call_id=tool_id,
            data={
                "tool_name": tool_name,
                "success": success,
                "elapsed": elapsed,
                "error": error,
            },
        )
        self._record_trace_event(
            "observation",
            tool_call_id=tool_id,
            data={
                "tool_name": tool_name,
                "success": success,
                "model_chars": len(getattr(result_views, "model", "") or ""),
                "ui_chars": len(getattr(result_views, "ui", "") or ""),
                "truncated": bool(getattr(result_views, "truncated", False)),
                "raw_ref": getattr(result_views, "raw_ref", None),
                "original_chars": getattr(result_views, "original_chars", 0),
            },
        )

    @staticmethod
    def _mark_runtime_tool_call_executing(runtime_tool_call: RuntimeToolCall | None) -> None:
        if runtime_tool_call is None:
            return
        if runtime_tool_call.status in {ToolCallStatus.VALIDATED, ToolCallStatus.POLICY_CHECKED}:
            runtime_tool_call.transition(ToolCallStatus.EXECUTING, reason="handler_start")

    @staticmethod
    def _finish_runtime_tool_call(runtime_tool_call: RuntimeToolCall | None, *, success: bool) -> None:
        if runtime_tool_call is None:
            return
        runtime_tool_call.result_success = success
        if runtime_tool_call.status == ToolCallStatus.EXECUTING:
            runtime_tool_call.transition(
                ToolCallStatus.SUCCEEDED if success else ToolCallStatus.FAILED,
                reason="handler_finished",
            )
        if runtime_tool_call.status in {
            ToolCallStatus.SUCCEEDED,
            ToolCallStatus.FAILED,
            ToolCallStatus.CANCELLED,
        }:
            runtime_tool_call.transition(ToolCallStatus.OBSERVED, reason="tool_result_queued")

    @staticmethod
    def _append_loop_stop_response(final_response: str, decision: LoopDecision) -> str:
        message = (
            f"[loop_stopped] status={decision.completion_status.value}; "
            f"reason={decision.reason}; iterations={decision.iteration}; "
            f"tool_calls={decision.tool_calls_used}."
        )
        if decision.human_intervention_required:
            message = f"{message} Human input or confirmation is required before continuing."
        if final_response:
            return f"{final_response}\n\n{message}"
        return message

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
        tool_id = str(tool_id or "").strip()
        if not tool_id:
            raise ValueError(f"tool result for {tool_name} requires a non-empty tool_call_id")
        if self._is_browser_or_vision_tool(tool_name):
            for item in pending_tool_results:
                if self._is_browser_or_vision_tool(item.get("tool_name", "")):
                    superseded_result = (
                        "[superseded_tool_result] This browser/vision frame was folded "
                        f"because a newer {tool_name} result follows in the same turn."
                    )
                    item["result"] = wrap_tool_observation_for_model(
                        tool_call_id=item.get("tool_use_id", ""),
                        tool_name=item.get("tool_name", "tool"),
                        content=superseded_result,
                    )
        pending_tool_results.append({
            "tool_use_id": tool_id,
            "tool_name": tool_name,
            "result": wrap_tool_observation_for_model(
                tool_call_id=tool_id,
                tool_name=tool_name,
                content=result,
                success=not self._tool_result_indicates_failure(result),
            ),
        })

    @staticmethod
    def _pending_tool_result_content(
        pending_tool_results: List[Dict[str, str]],
        tool_id: str,
    ) -> str:
        for item in pending_tool_results:
            if str(item.get("tool_use_id") or "") != str(tool_id or ""):
                continue
            wrapped = str(item.get("result") or "")
            marker = "content:\n"
            if marker in wrapped and "\n[/DATA_ONLY_TOOL_OBSERVATION]" in wrapped:
                wrapped = wrapped.split(marker, 1)[1].rsplit(
                    "\n[/DATA_ONLY_TOOL_OBSERVATION]",
                    1,
                )[0]
            return wrapped.strip()
        return ""

    @classmethod
    def _pending_tool_result_fingerprint(
        cls,
        pending_tool_results: List[Dict[str, str]],
        tool_id: str,
    ) -> str:
        content = cls._pending_tool_result_content(pending_tool_results, tool_id)
        if not content:
            return ""
        normalized = re.sub(r"tool_call_id:\s*[^\n]+", "tool_call_id:<normalized>", content)
        return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:20]

    @classmethod
    def _pending_web_evidence_sources(
        cls,
        pending_tool_results: List[Dict[str, str]],
        tool_id: str,
    ) -> List[Dict[str, str]]:
        content = cls._pending_tool_result_content(pending_tool_results, tool_id)
        try:
            payload = json.loads(content)
        except Exception:
            return []
        if not isinstance(payload, dict) or payload.get("ok") is not True or payload.get("grounded") is not True:
            return []
        sources: List[Dict[str, str]] = []
        for source in payload.get("sources") or []:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            sources.append({
                "title": str(source.get("title") or url).strip()[:240],
                "url": url,
            })
        return sources

    @staticmethod
    def _append_web_evidence_sources(
        final_response: str,
        sources: List[Dict[str, str]],
    ) -> str:
        seen: set[str] = set()
        lines: List[str] = []
        response = str(final_response or "").strip()
        for source in sources:
            url = str(source.get("url") or "").strip()
            if not url or url in seen or url in response:
                continue
            seen.add(url)
            title = str(source.get("title") or url).strip().replace("\n", " ")
            lines.append(f"- [{title}]({url})")
            if len(lines) >= 8:
                break
        if not lines:
            return response
        appendix = "依据来源（由检索工具返回）：\n" + "\n".join(lines)
        return f"{response}\n\n{appendix}" if response else appendix

    @staticmethod
    def _build_unverified_tool_response(decision: LoopDecision | None) -> str:
        reason = str(getattr(decision, "reason", "") or "").strip()
        message = "这次没有获得可验证的工具结果，因此我不能把未核实的内容当作事实回答。"
        if reason:
            message += f" 运行停止原因：{reason}。"
        return message

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
        runtime_tool_call: RuntimeToolCall | None = None,
    ) -> None:
        history_entry = {
            "run_id": getattr(runtime_tool_call, "run_id", getattr(self._run_state, "run_id", "")),
            "turn_id": getattr(runtime_tool_call, "turn_id", str(self._get_active_turn_id())),
            "tool_call_id": getattr(runtime_tool_call, "id", ""),
            "tool_name": tool_name,
            "input": redact_sensitive_data(tool_input),
            "result": result_views.log,
            "raw_ref": result_views.raw_ref,
            "truncated": result_views.truncated,
            "timestamp": time.time(),
        }
        if runtime_tool_call is not None:
            history_entry["source_provider"] = runtime_tool_call.source_provider
            history_entry["lifecycle_status"] = runtime_tool_call.status.value
            history_entry["lifecycle_trace"] = list(runtime_tool_call.trace)
            history_entry["result_success"] = runtime_tool_call.result_success
            history_entry["resources"] = [
                resource.to_dict()
                for resource in infer_tool_resources(runtime_tool_call.name, runtime_tool_call.arguments)
            ]
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
            reasoning_content = ""
            if response is not None and hasattr(response, "content"):
                rc = response.content
                if isinstance(rc, dict):
                    text_part = rc.get("text", "") or current_text
                    reasoning_content = str(rc.get("reasoning_content") or "")
                elif isinstance(rc, str):
                    text_part = rc or current_text
                else:
                    text_part = current_text
            else:
                text_part = current_text
            content = {
                "text": text_part,
                "tool_calls": tool_use_events,
            }
            if reasoning_content:
                content["reasoning_content"] = reasoning_content
            return content
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
        self._save_checkpoint(
            CheckpointStage.TURN_FINISHED,
            state={
                "status": "succeeded",
                "response_chars": len(final_response or ""),
                "consolidate_context": consolidate_context,
            },
        )
        self._record_trace_event(
            "final_status",
            data={
                "status": "succeeded",
                "response_chars": len(final_response or ""),
                "consolidate_context": consolidate_context,
            },
        )
        try:
            end_seq = self._history.snapshot()
        except Exception:
            end_seq = user_seq
        if emit_finished:
            event_bus.publish(FinishedEvent(turn_id=self._get_active_turn_id()))
        self._schedule_turn_completed_hooks(TurnCompletedContext(
            user_input=user_input,
            final_response=final_response,
            user_seq=user_seq,
            end_seq=end_seq,
            consolidate_context=consolidate_context,
        ))
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

"""
ConfirmGate — 工具调用确认门

职责：
  当工具需要用户确认时，挂起执行并通过 EventBus 通知 UI，
  等待 UI 返回结果后继续。

独立于 AgentEngine 运行，仅依赖 ToolRegistry 和 event_bus。
"""
import asyncio
import json
import logging
from typing import Callable, Optional

from pawmate.bridge.contracts import ToolConfirmEvent
from pawmate.core.redaction import redact_for_json, redact_sensitive_data, redact_text

APPROVAL_TIMEOUT_SECONDS = 120.0


class ConfirmGate:
    """工具确认门控：Event + 回调模式，跨线程安全"""

    def __init__(self, tool_registry, event_bus):
        """
        Args:
            tool_registry: ToolRegistry 实例（保存 ApprovalDecision、注册回调）
            event_bus: EventBus 实例（发送 tool_confirm 信号）
        """
        self._registry = tool_registry
        self._event_bus = event_bus

        self._event: Optional[asyncio.Event] = None
        self._result: bool = False
        self._reason: str = "pending"
        self._worker_loop: Optional[asyncio.AbstractEventLoop] = None
        self._explanation_provider: Optional[Callable] = None
        self._request_lock = asyncio.Lock()
        self._auto_approve = False

    # ── 公共接口 ──────────────────────────────────────────────

    def set_worker_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """设置 worker 线程的 event loop（用于跨线程 Event.set）。"""
        self._worker_loop = loop

    def setup(self) -> None:
        """在 ToolRegistry 上注册确认回调。需在 create() 中调用。"""
        self._registry.set_confirm_callback(self._make_handler())

    def set_explanation_provider(self, provider: Optional[Callable]) -> None:
        """Attach an optional async explanation provider for approval UI."""
        self._explanation_provider = provider

    def set_auto_approve(self, enabled: bool) -> None:
        """Enable or disable automatic approval for confirm-level tool calls."""
        self._auto_approve = bool(enabled)

    def resolve(self, allowed: bool) -> None:
        """
        从 UI 线程调用：设置确认结果并唤醒等待的 worker。
        使用 call_soon_threadsafe 确保跨线程安全。

        Args:
            allowed: True=允许执行, False=拒绝
        """
        from pawmate.tools.registry import ApprovalDecision

        _log = logging.getLogger("pawmate")

        evt = self._event
        if evt is None:
            _log.warning("[Approval] stale resolve ignored (no pending confirm)")
            return
        if evt.is_set():
            _log.warning("[Approval] stale resolve ignored (already resolved)")
            return

        self._result = allowed
        self._reason = "approved" if allowed else "denied"

        self._registry._last_approval = ApprovalDecision(
            allowed=allowed,
            reason=self._reason,
        )

        _log.info("[Approval] %s tool=(see prior log)", self._reason)

        loop = self._worker_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(evt.set)
        else:
            evt.set()  # 兜底：loop 不可用时退回旧行为

    # ── 内部实现 ──────────────────────────────────────────────

    def _make_handler(self):
        """Build the async confirmation callback registered on ToolRegistry."""
        async def _confirm_handler(name: str, input_data: dict) -> bool:
            return await self.request_confirmation(name, input_data)

        return _confirm_handler

    async def request_confirmation(self, name: str, input_data: dict) -> bool:
        """Request approval through the existing EventBus/UI confirmation flow."""
        from pawmate.tools.registry import ApprovalDecision

        async with self._request_lock:
            _log = logging.getLogger("pawmate")
            input_json = redact_for_json(input_data)
            _log.info("[Approval] request tool=%s", name)

            if self._auto_approve:
                self._registry._last_approval = ApprovalDecision(
                    allowed=True,
                    reason="auto_approved",
                )
                _log.info("[Approval] auto_approved tool=%s", name)
                return True

            # Create the wait object before notifying UI to avoid races.
            self._event = asyncio.Event()
            self._result = False
            self._reason = "pending"

            self._event_bus.publish(
                ToolConfirmEvent(name, await self._build_ui_payload(name, input_data, input_json))
            )

            try:
                await asyncio.wait_for(self._event.wait(), timeout=APPROVAL_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                self._reason = "timeout"
                self._registry._last_approval = ApprovalDecision(
                    allowed=False, reason="timeout"
                )
                _log.info("[Approval] timeout tool=%s", name)
                return False
            finally:
                self._event = None

            self._registry._last_approval = ApprovalDecision(
                allowed=self._result,
                reason=self._reason,
            )
            _log.info("[Approval] %s tool=%s", self._reason, name)
            return self._result

    async def _build_ui_payload(self, name: str, input_data: dict, input_json: str) -> str:
        explanation = await self._get_explanation(name, input_data)
        if not explanation:
            return input_json
        try:
            safe_input = json.loads(input_json)
        except Exception:
            safe_input = input_json
        return redact_for_json({
            "__pawmate_approval": True,
            "tool_input": safe_input,
            "explanation": explanation,
        })

    async def _get_explanation(self, name: str, input_data: dict) -> dict | None:
        if self._explanation_provider is None:
            return None
        try:
            result = self._explanation_provider(name, redact_sensitive_data(input_data))
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                result = await result
            if isinstance(result, dict):
                return {
                    "intent": self._clean_explain(result.get("intent", "")),
                    "plan": self._clean_explain(result.get("plan", "")),
                    "risk": self._clean_explain(result.get("risk", "")),
                }
            text = self._clean_explain(str(result or ""))
            return {"intent": text, "plan": "", "risk": ""}
        except Exception as exc:
            logging.getLogger("pawmate").warning("[Approval] explanation failed: %s", exc)
            return None

    @staticmethod
    def _clean_explain(text: str, limit: int = 220) -> str:
        text = redact_text(str(text or "")).strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

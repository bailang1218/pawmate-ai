"""Agent loop completion and recovery contracts."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CompletionStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


class RecoveryErrorType(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    POLICY_DENIED = "POLICY_DENIED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_EXCEPTION = "TOOL_EXCEPTION"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    OBSERVATION_TOO_LARGE = "OBSERVATION_TOO_LARGE"
    BROWSER_ELEMENT_NOT_FOUND = "BROWSER_ELEMENT_NOT_FOUND"
    USER_CONFIRM_REJECTED = "USER_CONFIRM_REJECTED"
    REPEATED_ACTION = "REPEATED_ACTION"


@dataclass(frozen=True)
class LoopDecision:
    should_continue: bool
    completion_status: CompletionStatus
    reason: str
    iteration: int = 0
    tool_calls_used: int = 0
    failure_count: int = 0
    repeated_action_count: int = 0
    human_intervention_required: bool = False
    error_type: RecoveryErrorType | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_continue": self.should_continue,
            "completion_status": self.completion_status.value,
            "reason": self.reason,
            "iteration": self.iteration,
            "tool_calls_used": self.tool_calls_used,
            "failure_count": self.failure_count,
            "repeated_action_count": self.repeated_action_count,
            "human_intervention_required": self.human_intervention_required,
            "error_type": self.error_type.value if self.error_type else "",
            "metadata": dict(self.metadata),
        }


class LoopController:
    """Small runtime-side guard for task loop termination."""

    def __init__(
        self,
        *,
        max_iterations: int,
        max_tool_calls: int,
        max_runtime_seconds: float | None = 300.0,
        failure_stop_threshold: int = 3,
        repeated_action_stop_threshold: int = 4,
    ) -> None:
        self.max_iterations = max(1, int(max_iterations or 1))
        self.max_tool_calls = max(0, int(max_tool_calls or 0))
        runtime_limit = float(max_runtime_seconds or 0.0)
        self.max_runtime_seconds = max(1.0, runtime_limit) if runtime_limit > 0 else None
        self.failure_stop_threshold = max(1, int(failure_stop_threshold or 1))
        self.repeated_action_stop_threshold = max(1, int(repeated_action_stop_threshold or 1))
        self.started_at = time.time()
        self.iteration = 0
        self.tool_calls_used = 0
        self.failure_count = 0
        self.repeated_action_count = 0
        self._action_counts: dict[str, int] = {}
        self._last_repetition_key = ""
        self.last_decision = LoopDecision(
            should_continue=True,
            completion_status=CompletionStatus.RUNNING,
            reason="started",
        )

    def before_iteration(self, tool_calls_used: int | None = None) -> LoopDecision:
        if tool_calls_used is not None:
            self.tool_calls_used = max(self.tool_calls_used, int(tool_calls_used or 0))
        if (
            self.max_runtime_seconds is not None
            and time.time() - self.started_at > self.max_runtime_seconds
        ):
            return self._stop(CompletionStatus.BUDGET_EXCEEDED, "max_runtime_seconds exceeded")
        if self.iteration >= self.max_iterations:
            return self._stop(CompletionStatus.BUDGET_EXCEEDED, "max_iterations exceeded")
        self.iteration += 1
        return self._continue("iteration_started")

    def record_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        success: bool,
        observation_fingerprint: str = "",
    ) -> LoopDecision:
        self.tool_calls_used += 1
        signature = tool_action_signature(tool_name, tool_input)
        action_count = self._action_counts.get(signature, 0) + 1
        self._action_counts[signature] = action_count
        repetition_key = f"{signature}:{observation_fingerprint}" if observation_fingerprint else signature
        if repetition_key == self._last_repetition_key:
            self.repeated_action_count += 1
        else:
            self._last_repetition_key = repetition_key
            self.repeated_action_count = 1

        if not success:
            self.failure_count += 1
        else:
            self.failure_count = 0

        if self.tool_calls_used > self.max_tool_calls:
            return self._stop(CompletionStatus.BUDGET_EXCEEDED, "max_tool_calls exceeded")
        if self.repeated_action_count >= self.repeated_action_stop_threshold:
            return self._stop(
                CompletionStatus.PARTIALLY_COMPLETED,
                "repeated identical tool action detected",
                error_type=RecoveryErrorType.REPEATED_ACTION,
                metadata={"signature": signature},
            )
        if self.failure_count >= self.failure_stop_threshold:
            return self._stop(
                CompletionStatus.FAILED,
                "consecutive tool failures exceeded threshold",
                error_type=RecoveryErrorType.TOOL_EXCEPTION,
            )
        return self._continue("tool_finished_task_still_running")

    def complete(self, status: CompletionStatus = CompletionStatus.COMPLETED, reason: str = "assistant_answered") -> LoopDecision:
        if status == CompletionStatus.RUNNING:
            raise ValueError("completion status cannot be RUNNING")
        return self._stop(status, reason)

    def needs_user_input(self, reason: str) -> LoopDecision:
        return self._stop(
            CompletionStatus.NEEDS_USER_INPUT,
            reason,
            human_intervention_required=True,
        )

    def pause_for_confirmation(self, reason: str = "confirmation required") -> LoopDecision:
        return self._stop(
            CompletionStatus.NEEDS_CONFIRMATION,
            reason,
            human_intervention_required=True,
        )

    def snapshot(self) -> dict[str, Any]:
        return self.last_decision.to_dict()

    def _continue(self, reason: str) -> LoopDecision:
        self.last_decision = LoopDecision(
            should_continue=True,
            completion_status=CompletionStatus.RUNNING,
            reason=reason,
            iteration=self.iteration,
            tool_calls_used=self.tool_calls_used,
            failure_count=self.failure_count,
            repeated_action_count=self.repeated_action_count,
        )
        return self.last_decision

    def _stop(
        self,
        status: CompletionStatus,
        reason: str,
        *,
        human_intervention_required: bool = False,
        error_type: RecoveryErrorType | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LoopDecision:
        self.last_decision = LoopDecision(
            should_continue=False,
            completion_status=status,
            reason=reason,
            iteration=self.iteration,
            tool_calls_used=self.tool_calls_used,
            failure_count=self.failure_count,
            repeated_action_count=self.repeated_action_count,
            human_intervention_required=human_intervention_required,
            error_type=error_type,
            metadata=metadata or {},
        )
        return self.last_decision


def tool_action_signature(tool_name: str, tool_input: dict[str, Any]) -> str:
    try:
        payload = json.dumps(tool_input or {}, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        payload = str(tool_input)
    return f"{tool_name}:{payload}"

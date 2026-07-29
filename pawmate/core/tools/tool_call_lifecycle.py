"""Runtime tool call lifecycle contract."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolCallStatus(str, Enum):
    CREATED_BY_MODEL = "created_by_model"
    PARSED = "parsed"
    VALIDATED = "validated"
    POLICY_CHECKED = "policy_checked"
    CONFIRM_PENDING = "confirm_pending"
    APPROVED = "approved"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OBSERVED = "observed"


_ALLOWED_TRANSITIONS: dict[ToolCallStatus, set[ToolCallStatus]] = {
    ToolCallStatus.CREATED_BY_MODEL: {ToolCallStatus.PARSED, ToolCallStatus.CANCELLED},
    ToolCallStatus.PARSED: {ToolCallStatus.VALIDATED, ToolCallStatus.FAILED},
    ToolCallStatus.VALIDATED: {
        ToolCallStatus.POLICY_CHECKED,
        ToolCallStatus.EXECUTING,
        ToolCallStatus.FAILED,
    },
    ToolCallStatus.POLICY_CHECKED: {
        ToolCallStatus.CONFIRM_PENDING,
        ToolCallStatus.APPROVED,
        ToolCallStatus.EXECUTING,
        ToolCallStatus.FAILED,
    },
    ToolCallStatus.CONFIRM_PENDING: {
        ToolCallStatus.APPROVED,
        ToolCallStatus.CANCELLED,
        ToolCallStatus.FAILED,
    },
    ToolCallStatus.APPROVED: {ToolCallStatus.EXECUTING},
    ToolCallStatus.EXECUTING: {
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.FAILED,
        ToolCallStatus.CANCELLED,
    },
    ToolCallStatus.SUCCEEDED: {ToolCallStatus.OBSERVED},
    ToolCallStatus.FAILED: {ToolCallStatus.OBSERVED},
    ToolCallStatus.CANCELLED: {ToolCallStatus.OBSERVED},
    ToolCallStatus.OBSERVED: set(),
}


@dataclass
class RuntimeToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str | None = None
    source_provider: str = ""
    run_id: str = ""
    turn_id: str = ""
    status: ToolCallStatus = ToolCallStatus.CREATED_BY_MODEL
    validation_result: dict[str, Any] | None = None
    policy_decision: dict[str, Any] | None = None
    result_success: bool | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.id = str(self.id or "").strip()
        self.name = str(self.name or "").strip()
        if not self.id:
            raise ValueError("runtime tool_call requires non-empty tool_call_id")
        if not self.name:
            raise ValueError("runtime tool_call requires non-empty tool name")
        if not isinstance(self.arguments, dict):
            raise ValueError("runtime tool_call arguments must be a dict")
        if not self.trace:
            self.trace.append(self._trace_event(self.status, reason="created"))

    @classmethod
    def from_provider_event(
        cls,
        event: dict[str, Any],
        *,
        run_id: str = "",
        turn_id: str = "",
    ) -> "RuntimeToolCall":
        call = cls(
            id=event.get("id", ""),
            name=event.get("name", ""),
            arguments=event.get("input", {}),
            raw_arguments=event.get("raw_arguments"),
            source_provider=str(event.get("provider") or ""),
            run_id=run_id,
            turn_id=turn_id,
        )
        call.transition(ToolCallStatus.PARSED, reason="provider_event")
        call.validation_result = {"ok": True, "stage": "runtime_shape"}
        call.transition(ToolCallStatus.VALIDATED, reason="runtime_shape")
        return call

    def transition(self, next_status: ToolCallStatus, *, reason: str = "") -> None:
        next_status = ToolCallStatus(next_status)
        allowed = _ALLOWED_TRANSITIONS[self.status]
        if next_status not in allowed:
            raise ValueError(f"invalid tool_call status transition: {self.status.value} -> {next_status.value}")
        self.status = next_status
        self.trace.append(self._trace_event(next_status, reason=reason))

    def to_assistant_tool_call(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "input": self.arguments,
        }

    def to_tool_call_request(self, *, log_gateway_start: bool = False) -> dict[str, Any]:
        return {
            "tool_name": self.name,
            "tool_input": self.arguments,
            "tool_id": self.id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "log_gateway_start": log_gateway_start,
            "runtime_tool_call": self,
        }

    def _trace_event(self, status: ToolCallStatus, *, reason: str = "") -> dict[str, Any]:
        return {
            "status": status.value,
            "reason": reason,
            "time": time.time(),
        }


def reject_duplicate_tool_call_id(tool_call_id: str, seen_ids: set[str]) -> None:
    if tool_call_id in seen_ids:
        raise ValueError(f"duplicate tool_call_id in assistant response: {tool_call_id}")
    seen_ids.add(tool_call_id)

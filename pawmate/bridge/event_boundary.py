"""Boundary metadata for runtime, UI, desktop-pet, and approval events."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pawmate.bridge.contracts import (
    AppEvent,
    ErrorEvent,
    EventKind,
    FinishedEvent,
    HeartbeatStatusEvent,
    HeartbeatWarningEvent,
    MaintenanceTickEvent,
    PresenceNudgeEvent,
    ProgressUpdateEvent,
    ScheduledFiredEvent,
    TaskRunningChangedEvent,
    TextDeltaEvent,
    ToolConfirmEvent,
    ToolDoneEvent,
    ToolErrorEvent,
    ToolNotifyEvent,
    ToolStartEvent,
    TurnCancelledEvent,
    TurnStartedEvent,
)
from pawmate.core.safety.redaction import redact_sensitive_data


class EventLayer(str, Enum):
    AGENT_RUNTIME = "agent_runtime"
    DESKTOP_UI = "desktop_ui"
    PET_STATE = "pet_state"
    BUBBLE_MESSAGE = "bubble_message"
    APPROVAL_UI = "approval_ui"
    SYSTEM_NOTIFICATION = "system_notification"


@dataclass(frozen=True)
class EventBoundary:
    event_id: str
    kind: str
    source_layer: EventLayer
    target_layer: EventLayer
    turn_id: int = 0
    can_enter_history: bool = False
    can_grant_permission: bool = False
    can_mutate_runtime_state: bool = False
    payload_summary: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "source_layer": self.source_layer.value,
            "target_layer": self.target_layer.value,
            "turn_id": self.turn_id,
            "can_enter_history": self.can_enter_history,
            "can_grant_permission": self.can_grant_permission,
            "can_mutate_runtime_state": self.can_mutate_runtime_state,
            "payload_summary": self.payload_summary,
            "created_at": self.created_at,
        }


def classify_event_boundary(event: AppEvent) -> EventBoundary:
    if not isinstance(event, AppEvent):
        raise TypeError("event boundary requires an AppEvent")

    source = EventLayer.AGENT_RUNTIME
    target = EventLayer.DESKTOP_UI
    can_mutate_runtime_state = False
    can_grant_permission = False

    if isinstance(event, (HeartbeatStatusEvent, HeartbeatWarningEvent, MaintenanceTickEvent)):
        source = EventLayer.SYSTEM_NOTIFICATION
        target = EventLayer.DESKTOP_UI
    elif isinstance(event, PresenceNudgeEvent):
        source = EventLayer.SYSTEM_NOTIFICATION
        target = EventLayer.BUBBLE_MESSAGE
    elif isinstance(event, (ToolConfirmEvent, ToolNotifyEvent)):
        source = EventLayer.AGENT_RUNTIME
        target = EventLayer.APPROVAL_UI
        can_grant_permission = isinstance(event, ToolConfirmEvent)
    elif isinstance(event, TaskRunningChangedEvent):
        can_mutate_runtime_state = False
    elif isinstance(event, ScheduledFiredEvent):
        target = EventLayer.AGENT_RUNTIME

    return EventBoundary(
        event_id=f"evt_{uuid.uuid4().hex}",
        kind=event.kind.value,
        source_layer=source,
        target_layer=target,
        turn_id=int(getattr(event, "turn_id", 0) or 0),
        can_enter_history=False,
        can_grant_permission=can_grant_permission,
        can_mutate_runtime_state=can_mutate_runtime_state,
        payload_summary=_payload_summary(event),
    )


def ensure_event_boundary(event: AppEvent) -> EventBoundary:
    boundary = classify_event_boundary(event)
    if boundary.can_enter_history:
        raise ValueError(f"UI/runtime event cannot enter conversation history: {boundary.kind}")
    return boundary


def _payload_summary(event: AppEvent) -> dict[str, Any]:
    if isinstance(event, TextDeltaEvent):
        return {"text_chars": len(event.text or "")}
    if isinstance(event, (ToolStartEvent, ToolDoneEvent, ToolErrorEvent, ToolNotifyEvent, ToolConfirmEvent)):
        return {
            "tool_name": getattr(event, "tool_name", ""),
            "payload_chars": len(str(getattr(event, "input_json", getattr(event, "result", "")) or "")),
        }
    if isinstance(event, ProgressUpdateEvent):
        return {
            "current_step": event.current_step,
            "total_steps": event.total_steps,
            "description_chars": len(event.description or ""),
        }
    if isinstance(event, ErrorEvent):
        return {"message_chars": len(event.message or "")}
    if isinstance(event, ScheduledFiredEvent):
        return redact_sensitive_data({"task_id": event.task_id, "action_chars": len(event.action or "")})
    if isinstance(event, (HeartbeatStatusEvent, HeartbeatWarningEvent, PresenceNudgeEvent, MaintenanceTickEvent)):
        return {"payload_chars": len(getattr(event, "payload_json", "") or "")}
    if isinstance(event, (FinishedEvent, TurnCancelledEvent, TurnStartedEvent, TaskRunningChangedEvent)):
        return redact_sensitive_data(event.__dict__ if hasattr(event, "__dict__") else {})
    return {"kind": EventKind(event.kind).value}

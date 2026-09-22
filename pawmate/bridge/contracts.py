"""Typed event contracts for cross-subsystem PawMate events.

This module is intentionally dependency-light: bridge primitives may import it
without pulling in core, UI, tools, memory, storage, or desktop-pet code.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar


class EventKind(str, Enum):
    TEXT_DELTA = "text_delta"
    TOOL_START = "tool_start"
    TOOL_DONE = "tool_done"
    TOOL_ERROR = "tool_error"
    TOOL_NOTIFY = "tool_notify"
    TOOL_CONFIRM = "tool_confirm"
    HEARTBEAT_STATUS = "heartbeat_status"
    HEARTBEAT_WARNING = "heartbeat_warning"
    PRESENCE_NUDGE = "presence_nudge"
    MAINTENANCE_TICK = "maintenance_tick"
    MODEL_RUNTIME = "model_runtime"
    SCHEDULED_FIRED = "scheduled_fired"
    FINISHED = "finished"
    PROGRESS_UPDATE = "progress_update"
    ERROR = "error"
    TURN_CANCELLED = "turn_cancelled"
    TURN_STARTED = "turn_started"
    TASK_RUNNING_CHANGED = "task_running_changed"


class AppEvent:
    """Marker base class for EventBus contracts."""

    kind: ClassVar[EventKind]


@dataclass(frozen=True, slots=True)
class TextDeltaEvent(AppEvent):
    text: str
    turn_id: int = 0

    kind: ClassVar[EventKind] = EventKind.TEXT_DELTA


@dataclass(frozen=True, slots=True)
class ToolStartEvent(AppEvent):
    tool_name: str
    input_json: str
    turn_id: int = 0

    kind: ClassVar[EventKind] = EventKind.TOOL_START


@dataclass(frozen=True, slots=True)
class ToolDoneEvent(AppEvent):
    tool_name: str
    result: str
    turn_id: int = 0

    kind: ClassVar[EventKind] = EventKind.TOOL_DONE


@dataclass(frozen=True, slots=True)
class ToolErrorEvent(AppEvent):
    tool_name: str
    error_msg: str
    turn_id: int = 0

    kind: ClassVar[EventKind] = EventKind.TOOL_ERROR


@dataclass(frozen=True, slots=True)
class ToolNotifyEvent(AppEvent):
    tool_name: str
    input_json: str

    kind: ClassVar[EventKind] = EventKind.TOOL_NOTIFY


@dataclass(frozen=True, slots=True)
class ToolConfirmEvent(AppEvent):
    tool_name: str
    input_json: str

    kind: ClassVar[EventKind] = EventKind.TOOL_CONFIRM


@dataclass(frozen=True, slots=True)
class HeartbeatStatusEvent(AppEvent):
    payload_json: str

    kind: ClassVar[EventKind] = EventKind.HEARTBEAT_STATUS


@dataclass(frozen=True, slots=True)
class HeartbeatWarningEvent(AppEvent):
    payload_json: str

    kind: ClassVar[EventKind] = EventKind.HEARTBEAT_WARNING


@dataclass(frozen=True, slots=True)
class PresenceNudgeEvent(AppEvent):
    payload_json: str

    kind: ClassVar[EventKind] = EventKind.PRESENCE_NUDGE


@dataclass(frozen=True, slots=True)
class MaintenanceTickEvent(AppEvent):
    payload_json: str

    kind: ClassVar[EventKind] = EventKind.MAINTENANCE_TICK


@dataclass(frozen=True, slots=True)
class ModelRuntimeEvent(AppEvent):
    payload_json: str

    kind: ClassVar[EventKind] = EventKind.MODEL_RUNTIME


@dataclass(frozen=True, slots=True)
class ScheduledFiredEvent(AppEvent):
    task_id: str
    action: str

    kind: ClassVar[EventKind] = EventKind.SCHEDULED_FIRED


@dataclass(frozen=True, slots=True)
class FinishedEvent(AppEvent):
    turn_id: int = 0

    kind: ClassVar[EventKind] = EventKind.FINISHED


@dataclass(frozen=True, slots=True)
class ProgressUpdateEvent(AppEvent):
    current_step: int
    total_steps: int
    description: str
    turn_id: int = 0

    kind: ClassVar[EventKind] = EventKind.PROGRESS_UPDATE


@dataclass(frozen=True, slots=True)
class ErrorEvent(AppEvent):
    message: str
    turn_id: int = 0

    kind: ClassVar[EventKind] = EventKind.ERROR


@dataclass(frozen=True, slots=True)
class TurnCancelledEvent(AppEvent):
    deleted_count: int
    turn_id: int = 0

    kind: ClassVar[EventKind] = EventKind.TURN_CANCELLED


@dataclass(frozen=True, slots=True)
class TurnStartedEvent(AppEvent):
    turn_id: int = 0

    kind: ClassVar[EventKind] = EventKind.TURN_STARTED


@dataclass(frozen=True, slots=True)
class TaskRunningChangedEvent(AppEvent):
    is_running: bool
    turn_id: int = 0

    kind: ClassVar[EventKind] = EventKind.TASK_RUNNING_CHANGED


EventPayload = dict[str, Any]

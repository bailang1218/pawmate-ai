from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class PetSignalType(str, Enum):
    USER_MESSAGE = "user_message"
    USER_UPLOAD = "user_upload"
    USER_PRAISE = "user_praise"
    USER_SCOLD = "user_scold"
    TURN_STARTED = "turn_started"
    SHORT_TASK = "short_task"
    MEMORY_TASK = "memory_task"
    TOOL_START = "tool_start"
    TOOL_DONE = "tool_done"
    TOOL_ERROR = "tool_error"
    PROGRESS_UPDATE = "progress_update"
    TEXT_DELTA = "text_delta"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    IDLE_SLEEP = "idle_sleep"
    WAKE_UP = "wake_up"
    MOUSE_TOUCH = "mouse_touch"


@dataclass(frozen=True)
class PetDataSignal:
    signal_type: PetSignalType
    turn_id: int | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class PetEventEmission:
    event_name: str
    turn_id: int | None
    reason: str


@dataclass
class _TurnState:
    turn_id: int | None = None
    active: bool = False
    has_tool: bool = False
    has_text: bool = False
    has_progress: bool = False
    work_committed: bool = False
    work_started_at: float | None = None
    work_output_started: bool = False
    last_frustrated_at: float | None = None
    light_tool_count: int = 0
    short_task_mode: str | None = None
    cancel_latched: bool = False


class PetEventAggregator:
    """Aggregate app/agent signals into a small desktop-pet event vocabulary."""

    LIGHT_TOOL_WORK_THRESHOLD = 3
    LONG_WORK_SECONDS = 15.0
    FRUSTRATION_COOLDOWN_SECONDS = 45.0

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._turn = _TurnState()
        self._clock = clock or time.monotonic

    def handle(self, signal: PetDataSignal) -> list[PetEventEmission]:
        signal_type = signal.signal_type
        payload = signal.payload or {}

        if signal_type == PetSignalType.USER_MESSAGE:
            return self._handle_user_message(signal.turn_id, payload)
        if signal_type == PetSignalType.USER_UPLOAD:
            return [self._emit("user_upload_received", signal.turn_id, "user uploaded context")]
        if signal_type == PetSignalType.USER_PRAISE:
            return [self._emit("user_praise", signal.turn_id, "user praise")]
        if signal_type == PetSignalType.USER_SCOLD:
            return [self._emit("sad", signal.turn_id, "user correction or scold")]
        if signal_type == PetSignalType.TURN_STARTED:
            self._turn = _TurnState(turn_id=signal.turn_id, active=True)
            return []
        if signal_type == PetSignalType.SHORT_TASK:
            if self._is_cancelled(signal.turn_id):
                return []
            self._turn.has_tool = True
            self._turn.light_tool_count += 1
            self._turn.short_task_mode = "short"
            if self._turn.work_committed:
                return [self._emit("processing", signal.turn_id, "short tool while working")]
            if self._turn.light_tool_count >= self.LIGHT_TOOL_WORK_THRESHOLD:
                self._mark_work_started()
                return [self._emit("task_start", signal.turn_id, "many light tools promoted to work")]
            return [self._emit("short_task", signal.turn_id, "short tool task")]
        if signal_type == PetSignalType.MEMORY_TASK:
            if self._is_cancelled(signal.turn_id):
                return []
            self._turn.has_tool = True
            self._turn.short_task_mode = "memory"
            if self._turn.work_committed:
                return [self._emit("processing", signal.turn_id, "memory tool while working")]
            return [self._emit("memory_task", signal.turn_id, "memory tool task")]
        if signal_type == PetSignalType.TOOL_START:
            if self._is_cancelled(signal.turn_id):
                return []
            first_tool = not self._turn.has_tool
            self._turn.has_tool = True
            self._mark_work_started()
            return [
                self._emit("task_start" if first_tool else "processing", signal.turn_id, "tool started")
            ]
        if signal_type == PetSignalType.TOOL_DONE:
            if self._is_cancelled(signal.turn_id):
                return []
            self._turn.has_progress = True
            return []
        if signal_type == PetSignalType.PROGRESS_UPDATE:
            if self._is_cancelled(signal.turn_id):
                return []
            if not self._turn.active:
                return []
            self._turn.has_progress = True
            was_working = self._turn.work_committed
            self._mark_work_started()
            if not was_working:
                return [self._emit("task_start", signal.turn_id, "progress started work")]
            if self._should_show_frustrated():
                return [self._emit("busy", signal.turn_id, "long-running work")]
            return [self._emit("processing", signal.turn_id, "progress update")]
        if signal_type == PetSignalType.TOOL_ERROR:
            if self._is_cancelled(signal.turn_id):
                return []
            return [self._emit("sad", signal.turn_id, "tool error")]
        if signal_type == PetSignalType.TEXT_DELTA:
            if self._is_cancelled(signal.turn_id) or self._turn.has_text:
                return []
            self._turn.has_text = True
            if self._turn.work_committed:
                self._turn.work_output_started = True
                event_name = "outputting"
            elif self._turn.short_task_mode == "memory":
                event_name = "memory_task"
            elif self._turn.has_tool:
                event_name = "short_task"
            else:
                event_name = "chat_normal"
            return [self._emit(event_name, signal.turn_id, "first assistant text")]
        if signal_type == PetSignalType.FINISHED:
            if self._is_cancelled(signal.turn_id):
                self._turn.active = False
                return []
            should_finish_work = self._turn.work_committed and not self._turn.work_output_started
            should_finish_chat = self._turn.has_text
            self._turn.active = False
            if should_finish_work:
                return [self._emit("work_complete", signal.turn_id, "turn finished after work")]
            if should_finish_chat:
                return [self._emit("chat_complete", signal.turn_id, "turn finished after chat output")]
            return []
        if signal_type == PetSignalType.CANCELLED:
            self._turn.cancel_latched = True
            self._turn.active = False
            return [self._emit("cancel", signal.turn_id, "turn cancelled")]
        if signal_type == PetSignalType.IDLE_SLEEP:
            if self._turn.active:
                return []
            return [self._emit("sleep", None, "idle sleep timer")]
        if signal_type == PetSignalType.WAKE_UP:
            return [self._emit("wake_up", signal.turn_id, "wake request")]
        if signal_type == PetSignalType.MOUSE_TOUCH:
            return [self._emit("wake_up", signal.turn_id, "mouse touch")]
        return []

    def _handle_user_message(self, turn_id: int | None, payload: dict[str, Any]) -> list[PetEventEmission]:
        if payload.get("repeat"):
            return [self._emit("repeat_question", turn_id, "repeat question")]
        if payload.get("hard"):
            return [self._emit("user_asks_hard", turn_id, "hard user question")]
        return [self._emit("user_input_received", turn_id, "user message")]

    def _is_cancelled(self, turn_id: int | None) -> bool:
        return (
            self._turn.cancel_latched
            and turn_id is not None
            and self._turn.turn_id == turn_id
        )

    def _mark_work_started(self) -> None:
        self._turn.work_committed = True
        if self._turn.work_started_at is None:
            self._turn.work_started_at = self._clock()

    def _should_show_frustrated(self) -> bool:
        started_at = self._turn.work_started_at
        if started_at is None:
            return False
        now = self._clock()
        if now - started_at < self.LONG_WORK_SECONDS:
            return False
        last = self._turn.last_frustrated_at
        if last is not None and now - last < self.FRUSTRATION_COOLDOWN_SECONDS:
            return False
        self._turn.last_frustrated_at = now
        return True

    @staticmethod
    def _emit(event_name: str, turn_id: int | None, reason: str) -> PetEventEmission:
        return PetEventEmission(event_name=event_name, turn_id=turn_id, reason=reason)

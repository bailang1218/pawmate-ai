"""ChatService: UI-facing multi-turn chat coordinator."""
from __future__ import annotations

import json
import logging
import time
import weakref
from typing import Any, Optional

from pawmate.qt_compat import QObject, QTimer, Signal, qt_receiver_count

from pawmate.bridge.contracts import (
    AppEvent,
    ErrorEvent,
    FinishedEvent,
    TaskRunningChangedEvent,
    TextDeltaEvent,
    ToolDoneEvent,
    ToolErrorEvent,
    ToolStartEvent,
    TurnStartedEvent,
)
from pawmate.bridge.event_bus import event_bus
from pawmate.core.redaction import redact_text


DEBUG_CHAT_LIFECYCLE = False


def _log_lifecycle(msg: str) -> None:
    if DEBUG_CHAT_LIFECYCLE:
        logging.getLogger("pawmate").info("[ChatService] %s", msg)


class ChatService(QObject):
    """Unified chat backend with multiple active user turns.

    Legacy signals are kept for older views. New Web UI wiring should prefer
    the *_turn signals so deltas, tool cards, and finalization cannot attach to
    the wrong assistant bubble.
    """

    text_delta = Signal(str)
    text_delta_turn = Signal(int, str)
    text_delta_turn_seq = Signal(int, int, str)
    finished = Signal()
    finished_turn = Signal(int)
    stream_end = Signal()
    stream_end_turn = Signal(int)
    error = Signal(str)
    error_turn = Signal(int, str)
    status_changed = Signal(str)
    tool_event = Signal(dict)
    tool_event_turn = Signal(dict)
    tasks_changed = Signal(str)
    cancelled = Signal()
    cancelled_turn = Signal(int)

    def __init__(
        self,
        config: dict,
        worker: Optional[QObject] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._logger = logging.getLogger("pawmate")
        self._mock_mode = config.get("ui", {}).get("web_ui_mock_mode", False)
        self._worker = worker
        self._busy = False
        self._stream_active = False
        self._event_bus_connected = False
        self._event_bus_handlers: list[tuple[object, object]] = []
        self._cancel_requested = False
        self._finished_already = False
        self._next_turn_id = 0
        self._current_turn_id = 0
        self._active_turns: dict[int, dict[str, Any]] = {}
        self._task_records: dict[int, dict[str, Any]] = {}
        self._stream_trace_counts: dict[int, int] = {}
        self._mock_timers: list[QTimer] = []
        self._timeout_timer: Optional[QTimer] = None

        runtime_cfg = config.get("runtime", {}) if isinstance(config.get("runtime", {}), dict) else {}
        try:
            timeout_seconds = int(runtime_cfg.get("task_timeout_seconds", 0))
        except Exception:
            timeout_seconds = 0
        self._task_timeout_seconds = max(0, timeout_seconds)
        self._queue_until_worker_ready = bool(runtime_cfg.get("queue_until_worker_ready", False))
        self._worker_ready_wait_ms = max(250, int(runtime_cfg.get("worker_ready_wait_ms", 5000)))
        self._max_concurrent_tasks = max(1, min(16, int(runtime_cfg.get("max_concurrent_tasks", 4))))

        self._logger.info("[ChatService] mock_mode=%s", self._mock_mode)
        self._logger.info("[ChatService] task_timeout=%ss", self._task_timeout_seconds)
        self_ref = weakref.ref(self)

        def cleanup_event_bus_handlers(*_args) -> None:
            service = self_ref()
            if service is not None:
                service._disconnect_event_bus()

        self._event_bus_cleanup = cleanup_event_bus_handlers
        self.destroyed.connect(self._event_bus_cleanup)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_listening(self) -> None:
        if self._mock_mode:
            self._logger.info("[ChatService] mock mode, skip EventBus connection")
            self.status_changed.emit("ready")
            return
        if self._event_bus_connected:
            return
        self._connect_event_bus()
        self._event_bus_connected = True
        self._logger.info("[ChatService] EventBus connected")
        if self._worker is not None:
            self._emit_ready_when_worker_ready(self._worker)

    def set_worker(self, worker: QObject) -> None:
        self._worker = worker
        if self._mock_mode:
            self._logger.debug("[ChatService] worker set (mock, ignored)")
            return
        has_consumers = (
            qt_receiver_count(self, self.text_delta, "text_delta(QString)") > 0
            or qt_receiver_count(self, self.finished, "finished()") > 0
            or qt_receiver_count(self, self.error, "error(QString)") > 0
        )
        if has_consumers and not self._event_bus_connected:
            self._connect_event_bus()
            self._event_bus_connected = True
            self._logger.info("[ChatService] EventBus connected via set_worker")
        self._emit_ready_when_worker_ready(worker)

    @staticmethod
    def _is_worker_ready(worker: QObject) -> bool:
        is_ready = getattr(worker, "is_ready", None)
        if callable(is_ready):
            try:
                return bool(is_ready())
            except Exception:
                return False
        return True

    def _emit_ready_when_worker_ready(self, worker: QObject) -> None:
        if self._is_worker_ready(worker):
            self.status_changed.emit("ready")
            return
        ready_signal = getattr(worker, "ready", None)
        connect = getattr(ready_signal, "connect", None)
        if callable(connect):
            try:
                connect(self._on_worker_ready)
            except Exception:
                pass

    def _on_worker_ready(self) -> None:
        self.status_changed.emit("ready")

    def submit_user_message(self, text: str) -> None:
        message = text.strip()
        if not message:
            self._logger.debug("ChatService: ignored empty message")
            return
        if len(self._active_turns) >= self._max_concurrent_tasks:
            self._logger.warning("[ChatService] too many concurrent tasks")
            self.error.emit("Too many concurrent tasks; wait for one to finish.")
            return

        self._logger.info(
            "[ChatService] %s submit: %.80s",
            "mock" if self._mock_mode else "real",
            message,
        )
        turn_id = self._begin_turn(message)
        if self._mock_mode:
            self._handle_mock(message, turn_id)
        else:
            self._handle_real(message, turn_id)

    def is_busy(self) -> bool:
        return bool(self._active_turns)

    def cancel_current_task(self) -> None:
        turn_id = self._current_turn_id
        if not turn_id and self._busy and not self._active_turns:
            turn_id = self._ensure_legacy_turn()
        if turn_id not in self._active_turns:
            self._logger.debug("[ChatService] cancel: no active latest turn")
            return

        self._logger.info("[ChatService] cancel_current_task turn=%s", turn_id)
        self._active_turns[turn_id]["cancel_requested"] = True
        self._cancel_requested = True
        for timer in list(self._active_turns[turn_id].get("mock_timers", [])):
            timer.stop()
        self._request_worker_cancel(turn_id)
        self.cancelled_turn.emit(turn_id)
        self.cancelled.emit()
        self._finalize_turn("cancelled", turn_id)

    # ------------------------------------------------------------------
    # Mock mode
    # ------------------------------------------------------------------

    def _handle_mock(self, message: str, turn_id: int) -> None:
        self._emit_tool_event({
            "type": "start",
            "name": "reading files",
            "detail": "engine.py / worker.py / config.json",
            "duration": "",
        }, turn_id)

        t1 = QTimer(self)
        t1.setSingleShot(True)
        t1.timeout.connect(lambda: self._emit_mock_tool_card(turn_id))
        t1.start(350)
        self._active_turns[turn_id]["mock_timers"].append(t1)

        t2 = QTimer(self)
        t2.setSingleShot(True)
        t2.timeout.connect(lambda: self._emit_mock_reply(message, turn_id))
        t2.start(1200)
        self._active_turns[turn_id]["mock_timers"].append(t2)

    def _emit_mock_tool_card(self, turn_id: int) -> None:
        if self._is_stale_turn(turn_id):
            return
        self._emit_tool_event({
            "type": "done",
            "name": "reading files",
            "detail": "engine.py / worker.py / config.json / registry.py / event_bus.py",
            "duration": "1.2s",
        }, turn_id)

    def _emit_mock_reply(self, message: str, turn_id: int) -> None:
        if self._is_stale_turn(turn_id):
            return
        reply = f"[Mock] Received: {message}\n\nMock mode is active; no real AI backend is connected."
        self._ensure_stream(turn_id)
        for chunk in self._chunk_text(reply):
            if chunk:
                self._emit_text_delta(chunk, turn_id)
        self._finalize_turn("finished", turn_id)

    # ------------------------------------------------------------------
    # Real mode
    # ------------------------------------------------------------------

    def _handle_real(self, message: str, turn_id: int) -> None:
        self._logger.debug("[ChatService] dispatch real message turn=%s: %.80s", turn_id, message)
        if self._worker is None:
            self._logger.error("[ChatService] worker not available for real submit")
            self._emit_error("Backend Worker is not available.", turn_id)
            self._finalize_turn("no_worker", turn_id)
            return
        if self._queue_until_worker_ready and not self._is_worker_ready(self._worker):
            self._logger.info("[ChatService] worker not ready; queue submit briefly")
            self.status_changed.emit("starting...")
            self._dispatch_when_worker_ready(message, turn_id, elapsed_ms=0)
            return
        self._send_to_worker(message, turn_id)

    def _dispatch_when_worker_ready(self, message: str, turn_id: int, *, elapsed_ms: int) -> None:
        if self._is_stale_turn(turn_id):
            return
        worker = self._worker
        if worker is None:
            self._emit_error("Backend Worker is not available.", turn_id)
            self._finalize_turn("no_worker", turn_id)
            return
        if self._is_worker_ready(worker):
            self._send_to_worker(message, turn_id)
            return
        if elapsed_ms >= self._worker_ready_wait_ms:
            self._logger.error("[ChatService] worker did not become ready after %sms", elapsed_ms)
            self._emit_error("Backend Worker did not become ready.", turn_id)
            self._finalize_turn("worker_not_ready", turn_id)
            return
        interval_ms = 50
        QTimer.singleShot(
            interval_ms,
            lambda: self._dispatch_when_worker_ready(
                message,
                turn_id,
                elapsed_ms=elapsed_ms + interval_ms,
            ),
        )

    # ------------------------------------------------------------------
    # EventBus relay handlers
    # ------------------------------------------------------------------

    def _connect_event_bus(self) -> None:
        if self._event_bus_handlers:
            return
        event_bus.event.connect(self._on_app_event)
        self._event_bus_handlers = [(event_bus.event, self._on_app_event)]

    def _disconnect_event_bus(self) -> None:
        for signal, handler in self._event_bus_handlers:
            try:
                signal.disconnect(handler)
            except (RuntimeError, TypeError, SystemError):
                pass
        self._event_bus_handlers.clear()
        self._event_bus_connected = False

    def _is_stale_turn(self, turn_id: int = 0) -> bool:
        if turn_id:
            state = self._active_turns.get(turn_id)
            return state is None or bool(state.get("finished")) or bool(state.get("cancel_requested"))
        if self._busy and not self._active_turns:
            return False
        return not self._active_turns

    def _is_stale_event(self, turn_id: int = 0) -> bool:
        return self._is_stale_turn(turn_id or self._current_turn_id)

    def _on_app_event(self, event: AppEvent) -> None:
        if isinstance(event, TextDeltaEvent):
            self._on_text_delta(event.text, event.turn_id)
        elif isinstance(event, ToolStartEvent):
            self._on_tool_start(event.tool_name, event.input_json, event.turn_id)
        elif isinstance(event, ToolDoneEvent):
            self._on_tool_done(event.tool_name, event.result, event.turn_id)
        elif isinstance(event, ToolErrorEvent):
            self._on_tool_error(event.tool_name, event.error_msg, event.turn_id)
        elif isinstance(event, FinishedEvent):
            self._on_finished(event.turn_id)
        elif isinstance(event, ErrorEvent):
            self._on_error(event.message, event.turn_id)

    def _on_text_delta(self, text: str, turn_id: int = 0) -> None:
        turn_id = turn_id or self._current_turn_id
        if not turn_id and self._busy and not self._active_turns:
            turn_id = self._ensure_legacy_turn()
        if self._is_stale_event(turn_id):
            _log_lifecycle("ignored text_delta stale")
            return
        if text:
            self._ensure_stream(turn_id)
            self._update_task(turn_id, status="answering", detail="streaming response")
            self._emit_text_delta(redact_text(text), turn_id)

    def _on_tool_start(self, name: str, input_json: str, turn_id: int = 0) -> None:
        turn_id = turn_id or self._current_turn_id
        if self._is_stale_event(turn_id):
            _log_lifecycle("ignored tool_start stale")
            return
        self._finalize_stream(turn_id)
        self.status_changed.emit("working...")
        self._update_task(turn_id, status="working", detail=f"tool: {name}")
        self._emit_tool_event({
            "type": "start",
            "name": name,
            "detail": redact_text(input_json),
            "duration": "",
        }, turn_id)

    def _on_tool_done(self, name: str, result: str, turn_id: int = 0) -> None:
        turn_id = turn_id or self._current_turn_id
        if self._is_stale_event(turn_id):
            _log_lifecycle("ignored tool_done stale")
            return
        self._update_task(turn_id, status="working", detail=f"done: {name}")
        self._emit_tool_event({
            "type": "done",
            "name": name,
            "detail": redact_text(result),
            "duration": "",
        }, turn_id)

    def _on_tool_error(self, name: str, error_msg: str, turn_id: int = 0) -> None:
        turn_id = turn_id or self._current_turn_id
        if self._is_stale_event(turn_id):
            _log_lifecycle("ignored tool_error stale")
            return
        self._update_task(turn_id, status="working", detail=f"tool error: {name}")
        self._emit_tool_event({
            "type": "error",
            "name": name,
            "detail": redact_text(error_msg),
            "duration": "",
        }, turn_id)

    def _on_finished(self, turn_id: int = 0) -> None:
        turn_id = turn_id or self._current_turn_id
        if self._is_stale_event(turn_id):
            _log_lifecycle("ignored finished stale")
            return
        self._logger.info("[ChatService] finished turn=%s", turn_id)
        self._finalize_turn("finished", turn_id)

    def _on_error(self, error_msg: str, turn_id: int = 0) -> None:
        turn_id = turn_id or self._current_turn_id
        if self._is_stale_event(turn_id):
            _log_lifecycle("ignored error stale")
            return
        self._logger.error("[ChatService] error turn=%s: %.200s", turn_id, error_msg)
        self._finalize_stream(turn_id)
        self._emit_error(redact_text(error_msg), turn_id)
        self._finalize_turn("error", turn_id)

    # ------------------------------------------------------------------
    # Timeouts
    # ------------------------------------------------------------------

    def _start_timeout(self, turn_id: int = 0) -> None:
        if self._task_timeout_seconds <= 0:
            _log_lifecycle("timeout disabled")
            return
        turn_id = turn_id or self._current_turn_id
        self._stop_timeout(turn_id)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._on_timeout(turn_id))
        timer.start(self._task_timeout_seconds * 1000)
        if turn_id in self._active_turns:
            self._active_turns[turn_id]["timeout_timer"] = timer
        self._timeout_timer = timer
        _log_lifecycle(f"timeout timer started ({self._task_timeout_seconds}s)")

    def _stop_timeout(self, turn_id: int = 0) -> None:
        timer = None
        if turn_id and turn_id in self._active_turns:
            timer = self._active_turns[turn_id].get("timeout_timer")
            self._active_turns[turn_id]["timeout_timer"] = None
        elif self._timeout_timer is not None:
            timer = self._timeout_timer
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
        if self._timeout_timer is timer:
            self._timeout_timer = None

    def _on_timeout(self, turn_id: int = 0) -> None:
        turn_id = turn_id or self._current_turn_id
        if self._is_stale_turn(turn_id):
            return
        self._logger.warning("[ChatService] task timeout turn=%s after %ss", turn_id, self._task_timeout_seconds)
        self._finalize_stream(turn_id)
        self._emit_error(f"Task timed out ({self._task_timeout_seconds}s); it has been stopped.", turn_id)
        self._request_worker_cancel(turn_id)
        self._finalize_turn("timeout", turn_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _begin_turn(self, message: str = "") -> int:
        self._next_turn_id += 1
        turn_id = self._next_turn_id
        self._current_turn_id = turn_id
        now = time.time()
        self._active_turns[turn_id] = {
            "stream_active": False,
            "finished": False,
            "cancel_requested": False,
            "timeout_timer": None,
            "mock_timers": [],
        }
        self._task_records[turn_id] = {
            "turn_id": turn_id,
            "title": (message or f"Task {turn_id}")[:80],
            "status": "thinking",
            "detail": "queued",
            "started_at": now,
            "updated_at": now,
        }
        self._sync_legacy_state()
        self.status_changed.emit("thinking...")
        self._start_timeout(turn_id)
        event_bus.publish(TaskRunningChangedEvent(True, turn_id=turn_id))
        event_bus.publish(TurnStartedEvent(turn_id=turn_id))
        self._emit_tasks_changed()
        self._logger.info("[Turn] started id=%s", turn_id)
        return turn_id

    def _ensure_stream(self, turn_id: int = 0) -> None:
        turn_id = turn_id or self._current_turn_id
        if not turn_id and self._busy and not self._active_turns:
            turn_id = self._ensure_legacy_turn()
        state = self._active_turns.get(turn_id)
        if state is None:
            return
        if not state.get("stream_active"):
            state["stream_active"] = True
            if turn_id == self._current_turn_id:
                self._stream_active = True
            if self._turn_seq_receiver_count() == 0:
                self.text_delta_turn.emit(turn_id, "")
            if turn_id == self._current_turn_id and not self._has_turn_stream_consumers():
                self.text_delta.emit("")

    def _finalize_stream(self, turn_id: int = 0) -> None:
        turn_id = turn_id or self._current_turn_id
        state = self._active_turns.get(turn_id)
        if state is None or not state.get("stream_active"):
            return
        state["stream_active"] = False
        self.stream_end_turn.emit(turn_id)
        if turn_id == self._current_turn_id and not self._has_turn_stream_consumers():
            self.stream_end.emit()
        if turn_id == self._current_turn_id:
            self._stream_active = False

    def _finalize_turn(self, reason: str, turn_id: int = 0) -> None:
        turn_id = turn_id or self._current_turn_id
        if not turn_id and self._busy and not self._active_turns:
            turn_id = self._ensure_legacy_turn()
        state = self._active_turns.get(turn_id)
        if state is None or state.get("finished"):
            return

        state["finished"] = True
        self._finalize_stream(turn_id)
        self._stop_timeout(turn_id)
        for timer in list(state.get("mock_timers", [])):
            timer.stop()
        self._active_turns.pop(turn_id, None)

        final_status = {
            "finished": "finished",
            "cancelled": "cancelled",
            "timeout": "timeout",
            "error": "error",
        }.get(reason, reason)
        self._update_task(turn_id, status=final_status, detail=reason, emit=False)
        self.finished_turn.emit(turn_id)
        if not self._has_turn_stream_consumers():
            self.finished.emit()
        self._sync_legacy_state()
        self.status_changed.emit("working..." if self._active_turns else "ready")
        event_bus.publish(TaskRunningChangedEvent(bool(self._active_turns), turn_id=turn_id))
        self._emit_tasks_changed()
        self._logger.info("[ChatService] finalize turn=%s reason=%s active=%d", turn_id, reason, len(self._active_turns))

    def _sync_legacy_state(self) -> None:
        self._busy = bool(self._active_turns)
        latest = self._active_turns.get(self._current_turn_id)
        self._finished_already = not self._busy
        self._cancel_requested = bool(latest and latest.get("cancel_requested"))
        self._stream_active = bool(latest and latest.get("stream_active"))

    def _ensure_legacy_turn(self) -> int:
        self._next_turn_id += 1
        turn_id = self._next_turn_id
        self._current_turn_id = turn_id
        self._active_turns[turn_id] = {
            "stream_active": self._stream_active,
            "finished": False,
            "cancel_requested": self._cancel_requested,
            "timeout_timer": None,
            "mock_timers": [],
        }
        now = time.time()
        self._task_records.setdefault(turn_id, {
            "turn_id": turn_id,
            "title": f"Task {turn_id}",
            "status": "working",
            "detail": "legacy active turn",
            "started_at": now,
            "updated_at": now,
        })
        return turn_id

    def _emit_text_delta(self, text: str, turn_id: int) -> None:
        count = self._stream_trace_counts.get(turn_id, 0) + 1
        self._stream_trace_counts[turn_id] = count
        turn_receivers = self._turn_delta_receiver_count()
        seq_receivers = self._turn_seq_receiver_count()
        if count <= 80 or count % 100 == 0:
            sample = text.replace("\n", "\\n")[:80]
            self._logger.info(
                "[StreamTrace] chat_service.emit turn=%s seq=%s turn_receivers=%s seq_receivers=%s legacy_receivers=%s len=%s text=%r",
                turn_id,
                count,
                turn_receivers,
                seq_receivers,
                qt_receiver_count(self, self.text_delta, "text_delta(QString)"),
                len(text),
                sample,
            )
        if seq_receivers:
            self.text_delta_turn_seq.emit(turn_id, count, text)
        elif turn_receivers:
            self.text_delta_turn.emit(turn_id, text)
        if turn_id == self._current_turn_id and not self._has_turn_stream_consumers():
            self.text_delta.emit(text)

    def _emit_error(self, message: str, turn_id: int) -> None:
        self.error_turn.emit(turn_id, message)
        if turn_id == self._current_turn_id:
            self.error.emit(message)

    def _emit_tool_event(self, payload: dict[str, Any], turn_id: int) -> None:
        data = dict(payload)
        data["turn_id"] = turn_id
        self.tool_event_turn.emit(data)
        self.tool_event.emit(data)

    def _has_turn_stream_consumers(self) -> bool:
        return (
            self._turn_delta_receiver_count() > 0
            or self._turn_seq_receiver_count() > 0
            or qt_receiver_count(self, self.stream_end_turn, "stream_end_turn(int)") > 0
            or qt_receiver_count(self, self.finished_turn, "finished_turn(int)") > 0
        )

    def _turn_delta_receiver_count(self) -> int:
        return qt_receiver_count(self, self.text_delta_turn, "text_delta_turn(int,QString)")

    def _turn_seq_receiver_count(self) -> int:
        return qt_receiver_count(self, self.text_delta_turn_seq, "text_delta_turn_seq(int,int,QString)")

    def _update_task(self, turn_id: int, *, status: str, detail: str = "", emit: bool = True) -> None:
        record = self._task_records.get(turn_id)
        if record is None:
            return
        record["status"] = status
        if detail:
            record["detail"] = detail[:160]
        record["updated_at"] = time.time()
        if emit:
            self._emit_tasks_changed()

    def _emit_tasks_changed(self) -> None:
        active_or_recent = sorted(
            self._task_records.values(),
            key=lambda item: float(item.get("started_at", 0)),
            reverse=True,
        )[:12]
        self.tasks_changed.emit(json.dumps(active_or_recent, ensure_ascii=False))

    def _send_to_worker(self, message: str, turn_id: int = 0) -> None:
        worker = self._worker
        if worker is None:
            return
        send = getattr(worker, "send_message")
        try:
            send(message, turn_id=turn_id or self._current_turn_id)
        except TypeError:
            try:
                send(message, turn_id or self._current_turn_id)
            except TypeError:
                send(message)

    def _request_worker_cancel(self, turn_id: int = 0) -> None:
        worker = self._worker
        if worker is None:
            return
        if hasattr(worker, "request_cancel"):
            try:
                worker.request_cancel(turn_id or self._current_turn_id)
            except TypeError:
                worker.request_cancel()
            except Exception as e:
                self._logger.warning("[ChatService] worker cancel failed: %s", e)
        elif hasattr(worker, "cancel_current_task"):
            try:
                worker.cancel_current_task()
            except Exception as e:
                self._logger.warning("[ChatService] worker cancel failed: %s", e)

    @staticmethod
    def _chunk_text(text: str, size: int = 8) -> list[str]:
        words = text.split(" ")
        chunks: list[str] = []
        buffer: list[str] = []
        char_count = 0
        for word in words:
            buffer.append(word)
            char_count += len(word) + 1
            if char_count >= size:
                chunks.append(" ".join(buffer))
                buffer = []
                char_count = 0
        if buffer:
            chunks.append(" ".join(buffer))
        return chunks

from __future__ import annotations

import argparse
import json
import logging
import time
from typing import Any

from pawmate.qt_compat import QObject, QTimer, Slot

from pawmate.bridge.contracts import (
    ErrorEvent,
    FinishedEvent,
    ProgressUpdateEvent,
    TextDeltaEvent,
    ToolDoneEvent,
    ToolErrorEvent,
    ToolStartEvent,
    TurnCancelledEvent,
    TurnStartedEvent,
)
from pawmate.bridge.event_bus import event_bus
from pawmate.desktop_pet.overlay import BubbleOverlay, PetInputBar
from pawmate.desktop_pet.app import apply_loop_defaults
from pawmate.desktop_pet.loops.sitting_work.config import SITTING_TALKING_ACTION_ID
from pawmate.desktop_pet.reply_surface import ReplySurfaceController
from pawmate.desktop_pet.runtime.controller import PetController
from pawmate.desktop_pet.runtime.fc_pet_signal_bridge import (
    FCPetSignalBridge,
    FunctionCallSignal,
    FunctionCallSignalType,
)
from pawmate.desktop_pet.runtime.pet_event_aggregator import (
    PetDataSignal,
    PetEventAggregator,
    PetSignalType,
)
from pawmate.desktop_pet.runtime.viewer import DesktopCarousel


class DesktopPetIntegration(QObject):
    """Own the desktop-pet window and route PawMate app signals into it."""

    def __init__(self, bridge: QObject | None = None, chat_service: QObject | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._logger = logging.getLogger("pawmate")
        self._bridge = bridge
        self._chat_service = chat_service
        self._main_window = parent
        self._controller = PetController()
        self._reply_controller = ReplySurfaceController(
            pet_enabled=lambda: self._enabled,
            surface=lambda: self._reply_surface,
            submit_fn=self._submit_user_message,
        )
        self._fc_bridge = FCPetSignalBridge()
        self._aggregator = PetEventAggregator()
        self._viewer: DesktopCarousel | None = None
        self._bubble: BubbleOverlay | None = None
        self._input_bar: PetInputBar | None = None
        self._enabled = False
        self._reply_surface = "window"
        self._bubble_streaming = False
        self._turn_id = 0
        self._wake_for_work_delay_ms = 1800
        self._min_user_reaction_ms = 900
        self._min_chat_visible_ms = 2200
        self._min_busy_visible_ms = 3200
        self._last_user_reaction_at_ms = 0.0
        self._last_chat_output_at_ms = 0.0
        self._last_busy_at_ms = 0.0
        self._waiting_for_chat_visible_start = False
        self._pending_chat_complete: tuple[int | None, str] | None = None
        self._cancelled_turn_ids: set[int] = set()

        if bridge is not None and hasattr(bridge, "userMessageSubmitted"):
            bridge.userMessageSubmitted.connect(self.on_user_message)
        if chat_service is not None and hasattr(chat_service, "cancelled"):
            chat_service.cancelled.connect(self.on_cancelled)

        event_bus.event.connect(self.on_app_event)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if enabled:
            self.start()
        else:
            self.stop()

    def set_reply_surface(self, surface: str) -> None:
        self._reply_surface = "bubble" if surface == "bubble" else "window"
        if not self._reply_controller.should_show_bubble():
            if self._bubble is not None:
                self._bubble.clear()
            self._bubble_streaming = False

    def start(self) -> None:
        if self._viewer is not None:
            return
        try:
            args = apply_loop_defaults(
                argparse.Namespace(
                    loop="standing",
                    source_root=None,
                    action=None,
                    mode="pet",
                    width_cm=4.0,
                    height_cm=6.0,
                    margin=24,
                    bridge_hold=5,
                    crossfade_frames=2,
                    cycles=None,
                    click_through=False,
                    click_lookaround_after=600.0,
                    compute_crop=False,
                    quit_after=None,
                    dry_run=False,
                    skip_initial_cues=True,
                )
            )
            self._viewer = DesktopCarousel(args)
            self._viewer.action_started_callback = self._on_pet_action_started
            self._viewer.set_window_moved_callback(self._reposition_overlays)
            self._controller.attach_viewer(self._viewer)
            self._bubble = BubbleOverlay(on_show_all=self._pop_main_window)
            self._input_bar = PetInputBar(on_submit=self._on_pet_input_submit)
            self._reposition_overlays()
            self._input_bar.show_bar()
            self._logger.info("[DesktopPet] started")
        except Exception as exc:
            self._destroy_overlays()
            if self._viewer is not None:
                try:
                    self._viewer.window.close()
                    self._viewer.window.deleteLater()
                except Exception:
                    pass
            self._viewer = None
            self._controller.attach_viewer(None)
            self._enabled = False
            self._logger.error("[DesktopPet] start failed: %s", exc, exc_info=True)

    def stop(self) -> None:
        if self._viewer is None:
            self._controller.attach_viewer(None)
            return
        try:
            for name in ("timer", "auto_idle_timer", "quit_timer"):
                timer = getattr(self._viewer, name, None)
                if timer is not None:
                    timer.stop()
            self._destroy_overlays()
            self._viewer.window.close()
            self._viewer.window.deleteLater()
            self._logger.info("[DesktopPet] stopped")
        except Exception as exc:
            self._logger.warning("[DesktopPet] stop failed: %s", exc)
        finally:
            self._viewer = None
            self._bubble_streaming = False
            self._controller.attach_viewer(None)

    @Slot(str)
    def on_user_message(self, text: str) -> None:
        payload = {
            "hard": _looks_hard(text),
            "repeat": _looks_repeat(text),
        }
        self._handle_pet_signal(PetDataSignal(PetSignalType.USER_MESSAGE, self._current_turn_id(), payload))

    @Slot(object)
    def on_app_event(self, event: object) -> None:
        if isinstance(event, TurnStartedEvent):
            self.on_turn_started(event.turn_id)
        elif isinstance(event, TextDeltaEvent):
            self.on_text_delta(event.text, event.turn_id)
        elif isinstance(event, ToolStartEvent):
            self.on_tool_start(event.tool_name, event.input_json, event.turn_id)
        elif isinstance(event, ToolDoneEvent):
            self.on_tool_done(event.tool_name, event.result, event.turn_id)
        elif isinstance(event, ToolErrorEvent):
            self.on_tool_error(event.tool_name, event.error_msg, event.turn_id)
        elif isinstance(event, ProgressUpdateEvent):
            self.on_progress_update(
                event.current_step,
                event.total_steps,
                event.description,
                event.turn_id,
            )
        elif isinstance(event, ErrorEvent):
            self.on_error(event.message, event.turn_id)
        elif isinstance(event, FinishedEvent):
            self.on_finished(event.turn_id)
        elif isinstance(event, TurnCancelledEvent):
            self.on_turn_cancelled(event.deleted_count, event.turn_id)

    @Slot()
    def on_turn_started(self, turn_id: int | None = None) -> None:
        if turn_id and turn_id > 0:
            self._turn_id = int(turn_id)
        else:
            self._turn_id += 1
        self._cancelled_turn_ids.discard(self._turn_id)
        self._bubble_streaming = False
        if self._reply_controller.should_show_bubble() and self._bubble is not None:
            self._bubble.begin_stream()
            self._bubble_streaming = True
            self._reposition_overlays()
        self._handle_fc_signal(FunctionCallSignal(FunctionCallSignalType.TURN_STARTED, turn_id=self._turn_id))

    @Slot(str)
    def on_text_delta(self, text: str, turn_id: int | None = None) -> None:
        if not text:
            return
        if self._reply_controller.should_show_bubble() and self._bubble is not None:
            if not self._bubble_streaming:
                self._bubble.begin_stream()
                self._bubble_streaming = True
            self._bubble.append_text(text)
            self._reposition_overlays()
        self._handle_fc_signal(
            FunctionCallSignal(
                FunctionCallSignalType.TEXT_DELTA,
                turn_id=self._event_turn_id(turn_id),
                payload={"chars": len(text)},
            )
        )

    @Slot(str, str)
    def on_tool_start(self, tool_name: str, input_json: str, turn_id: int | None = None) -> None:
        self._handle_fc_signal(
            FunctionCallSignal(
                FunctionCallSignalType.TOOL_USE,
                turn_id=self._event_turn_id(turn_id),
                tool_name=tool_name,
                tool_input=_loads_json_dict(input_json),
            )
        )

    @Slot(str, str)
    def on_tool_done(self, tool_name: str, _result: str, turn_id: int | None = None) -> None:
        self._handle_fc_signal(
            FunctionCallSignal(
                FunctionCallSignalType.TOOL_RESULT,
                turn_id=self._event_turn_id(turn_id),
                tool_name=tool_name,
            )
        )

    @Slot(str, str)
    def on_tool_error(self, tool_name: str, error_msg: str, turn_id: int | None = None) -> None:
        self._handle_fc_signal(
            FunctionCallSignal(
                FunctionCallSignalType.TOOL_ERROR,
                turn_id=self._event_turn_id(turn_id),
                tool_name=tool_name,
                payload={"error": error_msg},
            )
        )

    @Slot(int, int, str)
    def on_progress_update(
        self,
        current_step: int,
        total_steps: int,
        description: str,
        turn_id: int | None = None,
    ) -> None:
        self._handle_pet_signal(
            PetDataSignal(
                PetSignalType.PROGRESS_UPDATE,
                self._event_turn_id(turn_id),
                {
                    "current_step": current_step,
                    "total_steps": total_steps,
                    "description": description,
                },
            )
        )

    @Slot(str)
    def on_error(self, error_msg: str, turn_id: int | None = None) -> None:
        self._handle_pet_signal(
            PetDataSignal(PetSignalType.TOOL_ERROR, self._event_turn_id(turn_id), {"error": error_msg})
        )

    @Slot()
    def on_finished(self, turn_id: int | None = None) -> None:
        if self._bubble_streaming and self._bubble is not None:
            self._bubble.finalize()
        self._bubble_streaming = False
        self._handle_fc_signal(
            FunctionCallSignal(FunctionCallSignalType.FINISHED, turn_id=self._event_turn_id(turn_id))
        )

    @Slot()
    def on_cancelled(self, turn_id: int | None = None) -> None:
        resolved_turn_id = self._event_turn_id(turn_id)
        if resolved_turn_id in self._cancelled_turn_ids:
            return
        self._handle_fc_signal(FunctionCallSignal(FunctionCallSignalType.CANCELLED, turn_id=resolved_turn_id))
        self._cancelled_turn_ids.add(resolved_turn_id)

    @Slot(int)
    def on_turn_cancelled(self, _deleted_count: int, turn_id: int | None = None) -> None:
        self.on_cancelled(turn_id)

    def _handle_fc_signal(self, signal: FunctionCallSignal) -> None:
        for pet_signal in self._fc_bridge.to_pet_signals(signal):
            self._handle_pet_signal(pet_signal)

    def _handle_pet_signal(self, signal: PetDataSignal) -> None:
        if not self._enabled or self._viewer is None:
            return
        for emission in self._aggregator.handle(signal):
            self._logger.debug("[DesktopPet] %s -> %s", signal.signal_type.value, emission.event_name)
            self._emit_pet_event(emission.event_name, emission.turn_id, emission.reason)

    def _submit_user_message(self, text: str) -> object:
        self._maybe_pop_main_window()
        send_message = getattr(self._bridge, "sendMessage", None)
        if callable(send_message):
            return send_message(text)
        signal = getattr(self._bridge, "userMessageSubmitted", None)
        if signal is not None and hasattr(signal, "emit"):
            append_signal = getattr(self._bridge, "appendUserMessage", None)
            if append_signal is not None and hasattr(append_signal, "emit"):
                append_signal.emit(text)
            return signal.emit(text)
        submit = getattr(self._chat_service, "submit_user_message", None)
        if callable(submit):
            self.on_user_message(text)
            return submit(text)
        self._logger.warning("[DesktopPet] no submit entry available for pet input")
        return None

    def _on_pet_input_submit(self, text: str) -> object:
        return self._reply_controller.on_user_submit(text, "pet_input")

    def _maybe_pop_main_window(self) -> None:
        if self._reply_controller.should_pop_window():
            self._pop_main_window()

    def _pop_main_window(self) -> None:
        window = self._main_window
        if window is None:
            return
        for method_name in ("show", "raise_", "activateWindow"):
            method = getattr(window, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    self._logger.debug("[DesktopPet] main window %s failed", method_name, exc_info=True)

    def _reposition_overlays(self) -> None:
        if self._viewer is None:
            return
        geom = self._viewer.window.frameGeometry()
        if self._bubble is not None:
            self._bubble.reposition(geom)
        if self._input_bar is not None:
            self._input_bar.reposition(geom)

    def _destroy_overlays(self) -> None:
        for overlay in (self._bubble, self._input_bar):
            if overlay is None:
                continue
            try:
                overlay.hide()
                overlay.close()
                overlay.deleteLater()
            except Exception as exc:
                self._logger.debug("[DesktopPet] overlay cleanup failed: %s", exc)
        self._bubble = None
        self._input_bar = None

    def _current_turn_id(self) -> int:
        if self._turn_id <= 0:
            self._turn_id = 1
        return self._turn_id

    def _event_turn_id(self, turn_id: int | None) -> int:
        if turn_id and turn_id > 0:
            if self._turn_id <= 0:
                self._turn_id = int(turn_id)
            return int(turn_id)
        return self._current_turn_id()

    def _on_pet_action_started(self, action_id: str) -> None:
        if action_id == SITTING_TALKING_ACTION_ID:
            self._last_chat_output_at_ms = time.monotonic() * 1000
            self._waiting_for_chat_visible_start = False
            if self._pending_chat_complete is not None:
                turn_id, reason = self._pending_chat_complete
                self._pending_chat_complete = None
                QTimer.singleShot(
                    self._min_chat_visible_ms,
                    lambda tid=turn_id, why=reason: self._emit_if_current("chat_complete", tid, why),
                )

    def _emit_pet_event(self, event_name: str, turn_id: int | None, reason: str) -> None:
        now_ms = time.monotonic() * 1000
        viewer_loop = getattr(self._viewer, "loop_name", None)

        if event_name in {"user_input_received", "user_asks_hard", "repeat_question"}:
            self._last_user_reaction_at_ms = now_ms

        if event_name == "thinking" and now_ms - self._last_user_reaction_at_ms < self._min_user_reaction_ms:
            return

        if event_name == "cancel":
            self._waiting_for_chat_visible_start = False
            self._pending_chat_complete = None

        if viewer_loop == "sitting_work" and event_name == "outputting":
            self._waiting_for_chat_visible_start = True
            self._pending_chat_complete = None

        if event_name in {"chat_normal", "short_task", "memory_task", "outputting"} and not (
            viewer_loop == "sitting_work" and event_name == "outputting"
        ):
            self._last_chat_output_at_ms = now_ms

        if event_name == "busy":
            if now_ms - self._last_busy_at_ms < self._min_busy_visible_ms:
                return
            self._last_busy_at_ms = now_ms

        if event_name == "chat_complete":
            if viewer_loop == "sitting_work" and self._waiting_for_chat_visible_start:
                self._pending_chat_complete = (turn_id, reason)
                return
            elapsed_ms = now_ms - self._last_chat_output_at_ms
            delay_ms = max(0, round(self._min_chat_visible_ms - elapsed_ms))
            if delay_ms > 0:
                QTimer.singleShot(delay_ms, lambda event=event_name, tid=turn_id, why=reason: self._emit_if_current(event, tid, why))
                return

        self._emit_if_current(event_name, turn_id, reason)

    def _emit_if_current(
        self,
        event_name: str,
        turn_id: int | None,
        reason: str,
        *,
        allow_sleeping_work_handoff: bool = True,
    ) -> None:
        if not self._enabled or self._viewer is None:
            return
        if turn_id is not None:
            if turn_id != self._current_turn_id() or turn_id in self._cancelled_turn_ids:
                return
        if allow_sleeping_work_handoff and self._emit_sleeping_work_handoff(event_name, turn_id, reason):
            return
        self._controller.emit(event_name, {"turn_id": turn_id, "reason": reason})

    def _emit_sleeping_work_handoff(self, event_name: str, turn_id: int | None, reason: str) -> bool:
        if event_name != "task_start" or self._viewer is None:
            return False
        if getattr(self._viewer, "loop_name", None) != "sleeping":
            return False
        self._controller.emit("wake_up", {"turn_id": turn_id, "reason": "wake before work"})
        QTimer.singleShot(
            self._wake_for_work_delay_ms,
            lambda event=event_name, tid=turn_id, why=reason: self._emit_if_current(
                event,
                tid,
                why,
                allow_sleeping_work_handoff=False,
            ),
        )
        return True


def _loads_json_dict(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _looks_hard(text: str) -> bool:
    lowered = text.lower()
    strong_markers = (
        "\u600e\u4e48\u8bbe\u8ba1",
        "\u67b6\u6784",
        "\u590d\u6742",
        "\u96be",
        "\u6839\u6e90",
        "\u539f\u7406",
        "\u7b97\u6cd5",
        "\u65b9\u6848",
        "\u7cfb\u7edf\u6027",
        "\u9c81\u68d2",
        "\u6392\u67e5",
        "debug",
        "bug",
    )
    if any(marker in lowered for marker in strong_markers):
        return True
    if "\u4e3a\u4ec0\u4e48" in lowered:
        technical_markers = (
            "\u70b8",
            "\u62a5\u9519",
            "\u5931\u8d25",
            "\u4e2d\u65ad",
            "\u5361",
            "\u903b\u8f91",
            "\u673a\u5236",
            "\u539f\u56e0",
        )
        return any(marker in lowered for marker in technical_markers)
    return False


def _looks_repeat(text: str) -> bool:
    markers = (
        "\u8fd8\u662f",
        "\u53c8",
        "\u91cd\u590d",
        "\u521a\u624d",
        "\u7b2c\u4e8c\u6b21",
        "\u518d\u95ee",
    )
    return any(marker in text for marker in markers)

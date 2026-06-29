"""Application signal wiring helpers."""
from __future__ import annotations

from pawmate.bridge.contracts import (
    HeartbeatStatusEvent,
    HeartbeatWarningEvent,
    MaintenanceTickEvent,
    PresenceNudgeEvent,
    TaskRunningChangedEvent,
    ToolConfirmEvent,
    TurnCancelledEvent,
)
from pawmate.bridge.event_bus import event_bus
from pawmate.core.redaction import redact_text


_CONNECTED_SIGNAL_SLOTS: set[tuple[int, tuple[object, ...]]] = set()


def _slot_key(slot) -> tuple[object, ...]:
    owner = getattr(slot, "__self__", None)
    func = getattr(slot, "__func__", None)
    if owner is not None and func is not None:
        return ("bound", id(owner), id(func))
    return ("callable", id(slot))


def _connection_key(signal, slot) -> tuple[int, tuple[object, ...]]:
    return (id(signal), _slot_key(slot))


def _connect_once(signal, slot) -> None:
    """Connect a Qt signal to a slot once without probing Qt via disconnect()."""
    key = _connection_key(signal, slot)
    if key in _CONNECTED_SIGNAL_SLOTS:
        return
    connected = getattr(signal, "connected", None)
    if connected is not None and slot in connected:
        _CONNECTED_SIGNAL_SLOTS.add(key)
        return
    signal.connect(slot)
    _CONNECTED_SIGNAL_SLOTS.add(key)


def _disconnect_all(signal, slot) -> None:
    key = _connection_key(signal, slot)
    connected = getattr(signal, "connected", None)
    if connected is not None:
        while slot in connected:
            signal.disconnect(slot)
        _CONNECTED_SIGNAL_SLOTS.discard(key)
        return
    if key not in _CONNECTED_SIGNAL_SLOTS:
        return
    try:
        signal.disconnect(slot)
    except (RuntimeError, TypeError, ValueError, SystemError):
        pass
    finally:
        _CONNECTED_SIGNAL_SLOTS.discard(key)


def _relay_event_to_web_bridge(bridge, event) -> None:
    if isinstance(event, HeartbeatStatusEvent):
        bridge.heartbeatStatus.emit(event.payload_json)
    elif isinstance(event, HeartbeatWarningEvent):
        bridge.heartbeatWarning.emit(event.payload_json)
    elif isinstance(event, PresenceNudgeEvent):
        bridge.presenceNudge.emit(event.payload_json)
    elif isinstance(event, MaintenanceTickEvent):
        bridge.maintenanceTick.emit(event.payload_json)
    elif isinstance(event, TurnCancelledEvent):
        bridge.turnCancelled.emit(event.deleted_count)
    elif isinstance(event, TaskRunningChangedEvent):
        bridge.taskRunningChanged.emit(event.is_running)
    elif isinstance(event, ToolConfirmEvent):
        bridge.toolConfirmRequest.emit(event.tool_name, event.input_json)


def wire_web_chat_bridge(app, bridge, chat_service) -> None:
    """Wire WebBridge, ChatService, and global EventBus events."""
    if getattr(bridge, "_pawmate_web_chat_bridge_wired", False):
        chat_service.start_listening()
        return
    bridge._pawmate_web_chat_bridge_wired = True

    _connect_once(bridge.userMessageSubmitted, chat_service.submit_user_message)

    if hasattr(chat_service, "text_delta_turn_seq") and hasattr(bridge, "relayAssistantDeltaForTurnSeq"):
        _connect_once(chat_service.text_delta_turn_seq, bridge.relayAssistantDeltaForTurnSeq)
        if hasattr(chat_service, "text_delta_turn") and hasattr(bridge, "relayAssistantDeltaForTurn"):
            _disconnect_all(chat_service.text_delta_turn, bridge.relayAssistantDeltaForTurn)
        _connect_once(chat_service.finished_turn, bridge.finalizeAssistantTurn)
        _connect_once(chat_service.stream_end_turn, bridge.finalizeAssistantTurn)
    elif hasattr(chat_service, "text_delta_turn") and hasattr(bridge, "relayAssistantDeltaForTurn"):
        _connect_once(chat_service.text_delta_turn, bridge.relayAssistantDeltaForTurn)
        _connect_once(chat_service.finished_turn, bridge.finalizeAssistantTurn)
        _connect_once(chat_service.stream_end_turn, bridge.finalizeAssistantTurn)
    else:
        _connect_once(chat_service.text_delta, bridge.relayAssistantDelta)
        _connect_once(chat_service.finished, bridge.finalizeAssistant)
        _connect_once(chat_service.stream_end, bridge.finalizeAssistant)

    if hasattr(chat_service, "tasks_changed") and hasattr(bridge, "relayTaskList"):
        _connect_once(chat_service.tasks_changed, bridge.relayTaskList)

    def relay_app_event(event, target=bridge):
        _relay_event_to_web_bridge(target, event)

    bridge._pawmate_app_event_relay = relay_app_event
    _connect_once(event_bus.event, relay_app_event)

    _connect_once(chat_service.status_changed, bridge.relayStatus)
    if hasattr(chat_service, "error_turn") and hasattr(bridge, "appendToolCardForTurn"):
        chat_service.error_turn.connect(
            lambda turn_id, msg: bridge.appendToolCardForTurn.emit(
                int(turn_id or 0), "Error", redact_text(msg), "error", ""
            )
        )
    else:
        chat_service.error.connect(
            lambda msg: bridge.appendToolCard.emit("Error", redact_text(msg), "error", "")
        )

    _connect_once(bridge.cancelRequested, chat_service.cancel_current_task)

    _connect_once(bridge.toolConfirmResolved, app._on_tool_confirm_resolved)

    if hasattr(chat_service, "cancelled_turn") and hasattr(bridge, "appendToolCardForTurn"):
        chat_service.cancelled_turn.connect(
            lambda turn_id: bridge.appendToolCardForTurn.emit(
                int(turn_id or 0), "Task cancelled", "User cancelled the task.", "warning", ""
            )
        )
    else:
        chat_service.cancelled.connect(
            lambda: bridge.appendToolCard.emit("Task cancelled", "User cancelled the task.", "warning", "")
        )

    if hasattr(chat_service, "tool_event_turn") and hasattr(bridge, "appendToolCardForTurn"):
        chat_service.tool_event_turn.connect(
            lambda payload: bridge.appendToolCardForTurn.emit(
                int(payload.get("turn_id") or 0),
                payload.get("name", "tool"),
                payload.get("detail", ""),
                payload.get("type", "done"),
                payload.get("duration", ""),
            )
        )
    else:
        chat_service.tool_event.connect(app._relay_tool_event_to_web)

    chat_service.start_listening()

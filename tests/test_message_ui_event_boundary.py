import pytest

from pawmate.bridge.contracts import (
    HeartbeatStatusEvent,
    PresenceNudgeEvent,
    TaskRunningChangedEvent,
    TextDeltaEvent,
    ToolConfirmEvent,
)
from pawmate.bridge.event_boundary import EventLayer, classify_event_boundary
from pawmate.bridge.event_bus import EventBus


def test_heartbeat_event_is_system_notification_not_history_message():
    boundary = classify_event_boundary(HeartbeatStatusEvent('{"status":"idle"}'))

    assert boundary.source_layer == EventLayer.SYSTEM_NOTIFICATION
    assert boundary.target_layer == EventLayer.DESKTOP_UI
    assert boundary.can_enter_history is False
    assert boundary.can_grant_permission is False


def test_presence_nudge_event_is_bubble_not_user_message():
    boundary = classify_event_boundary(PresenceNudgeEvent('{"text":"still here"}'))

    assert boundary.source_layer == EventLayer.SYSTEM_NOTIFICATION
    assert boundary.target_layer == EventLayer.BUBBLE_MESSAGE
    assert boundary.can_enter_history is False


def test_approval_event_targets_approval_ui_without_mutating_runtime_state():
    boundary = classify_event_boundary(ToolConfirmEvent("delete_path", '{"path":"x"}'))

    assert boundary.source_layer == EventLayer.AGENT_RUNTIME
    assert boundary.target_layer == EventLayer.APPROVAL_UI
    assert boundary.can_grant_permission is True
    assert boundary.can_mutate_runtime_state is False


def test_runtime_text_delta_is_ui_output_event_not_conversation_history():
    boundary = classify_event_boundary(TextDeltaEvent("assistant text", turn_id=12))

    assert boundary.source_layer == EventLayer.AGENT_RUNTIME
    assert boundary.target_layer == EventLayer.DESKTOP_UI
    assert boundary.turn_id == 12
    assert boundary.can_enter_history is False
    assert boundary.payload_summary["text_chars"] == len("assistant text")


def test_event_bus_records_last_boundary_and_rejects_raw_payloads():
    bus = EventBus()
    received = []
    bus.subscribe(TextDeltaEvent, lambda event: received.append(event.text))

    bus.publish(TextDeltaEvent("hello", turn_id=7))

    assert received == ["hello"]
    last = bus.get_last_boundary()
    assert last["kind"] == "text_delta"
    assert last["turn_id"] == 7
    assert last["can_enter_history"] is False

    with pytest.raises(TypeError, match="AppEvent"):
        bus.publish({"kind": "text_delta"})  # type: ignore[arg-type]


def test_task_running_changed_event_cannot_directly_mutate_runtime_state():
    boundary = classify_event_boundary(TaskRunningChangedEvent(True, turn_id=3))

    assert boundary.kind == "task_running_changed"
    assert boundary.target_layer == EventLayer.DESKTOP_UI
    assert boundary.can_mutate_runtime_state is False

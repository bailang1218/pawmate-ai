# UI and Desktop Event Boundary

## Responsibility

UI, desktop-pet, heartbeat, bubble, approval, and runtime events are typed app
events. They are not conversation messages and cannot mutate runtime state by
pretending to be user input.

## Inputs

- Runtime text deltas and final events.
- Tool start/done/error events.
- Tool notify/confirm events.
- Heartbeat status/warnings and presence nudges.
- Desktop-pet state cues.

## Outputs

- EventBus dispatch.
- Qt signal delivery.
- Event boundary metadata with source layer, target layer, turn ID, and
  permission/history flags.

## Invariants

- App events must be `AppEvent` instances.
- Heartbeat and presence events cannot enter conversation history.
- Presence nudges are bubble messages, not user messages.
- Approval UI can return confirm decisions only through ConfirmGate.
- Pet state changes cannot grant tool permission.

## Forbidden

- Bubble or heartbeat text appended as role `user`.
- UI directly mutating runtime core state.
- Pet persona overriding safety/policy.

## Tests

- `tests/test_message_ui_event_boundary.py`

## Code Paths

- `pawmate/bridge/contracts.py`
- `pawmate/bridge/event_boundary.py`
- `pawmate/bridge/event_bus.py`
- `pawmate/core/heartbeat.py`
- `pawmate/core/chat_service.py`

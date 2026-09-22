# Message Role Contract

## Responsibility

Message roles preserve provenance across system, user, assistant, and tool
boundaries before provider-specific conversion.

## Inputs

- Real user text.
- Assistant text and assistant tool calls.
- Tool observations bound to tool call IDs.
- Internal continuation data for provider retries.

## Outputs

- HistoryStore messages.
- Provider-formatted messages.
- Tool result messages with role `tool`.

## Invariants

- `user` role is only real user input.
- `assistant` role is model output or assistant tool-call metadata.
- `tool` role is only tool result data and must include `tool_call_id`.
- Internal retry continuation uses assistant/system continuation, not user.
- Missing tool IDs fail hard.

## Forbidden

- Tool results falling back to user messages.
- Runtime control messages disguised as user input.
- Orphan tool results silently entering provider context.

## Tests

- `tests/test_message_role_boundary.py`
- `tests/test_observation_boundary.py`
- `tests/test_provider_boundary.py`

## Code Paths

- `pawmate/storage/history_store.py`
- `pawmate/core/model/provider_runner.py`
- `pawmate/core/providers/*_provider.py`
- `pawmate/core/runtime/engine.py`

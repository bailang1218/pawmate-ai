# Tool Call Lifecycle Contract

## Responsibility

Tool calls are runtime lifecycle objects, not anonymous provider dictionaries.
They carry IDs, validation state, policy state, execution state, trace, and
result status.

## Inputs

- Normalized provider `tool_use` events.
- Textual protocol fallback tool calls.
- ToolRegistry schema and metadata.

## Outputs

- Assistant tool-call history entries.
- Tool execution requests.
- Tool result observations.
- Tool lifecycle trace entries.

## Invariants

- `tool_call_id` is required and unique per assistant response.
- Arguments must be a dict before execution.
- Status transitions are constrained.
- Execution completion moves to `observed` only after result queueing.
- Parallel tool calls acquire resource locks.

## Forbidden

- Duplicate IDs in one model response.
- Handler execution with invalid runtime tool-call shape.
- Treating `tool_finished` as `task_finished`.

## Tests

- `tests/test_tool_call_lifecycle.py`
- `tests/test_tool_call_state_boundary.py`
- `tests/test_tool_call_loop_boundary.py`

## Code Paths

- `pawmate/core/tools/tool_call_lifecycle.py`
- `pawmate/core/tools/tool_call_runner.py`
- `pawmate/core/runtime_state.py`
- `pawmate/core/loop_control.py`
- `pawmate/core/runtime/engine.py`

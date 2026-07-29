# Trace, Replay, and Checkpoint Contract

## Responsibility

Trace explains why a runtime step happened. Checkpoints preserve enough durable
state to inspect and resume cautiously without automatically replaying side
effects.

## Inputs

- Run and turn IDs.
- Prompt section trace.
- Provider request/response summaries.
- Tool lifecycle, policy, confirm, observation, and loop decisions.
- Checkpoint stages for turn and tool execution.

## Outputs

- `AgentRunTrace` JSON.
- Provider attempt trace.
- Tool call history with run/turn/tool IDs.
- Checkpoint files under data/checkpoints.

## Invariants

- Trace redacts secrets.
- Every tool call can be explained by tool_call_id.
- Provider retry/fallback attempts are recorded.
- Checkpoints default `replay_allowed=false`.
- Side-effect checkpoints include idempotency key and side-effect metadata.

## Forbidden

- Durable replay automatically re-executing destructive actions.
- Trace leaking tokens, passwords, or API keys.
- Provider fallback happening without an attempt trace.

## Tests

- `tests/test_tool_call_trace_boundary.py`
- `tests/test_tool_call_checkpoint_boundary.py`
- `tests/test_provider_boundary.py`

## Code Paths

- `pawmate/core/observability/trace.py`
- `pawmate/core/checkpoint/checkpoint.py`
- `pawmate/core/model/provider_runner.py`
- `pawmate/core/runtime/engine.py`

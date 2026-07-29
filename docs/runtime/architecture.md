# PawMate Runtime Architecture Contract

Status: Runtime Boundary Hardening v0.2

## Responsibility

PawMate Runtime owns the boundary between real user input, model messages,
tools, observations, policy, trace, UI events, and durable checkpoints.
Runtime rules must be enforced by code contracts before they are described in
prompt text.

## Main Chain

```text
real user input
-> AgentEngine.chat
-> TurnContextBuilder / PromptSection
-> HistoryStore message window
-> ToolRegistry.list_tools
-> ProviderRunner / provider adapters
-> RuntimeToolCall lifecycle
-> ToolRegistry.execute
-> ToolObservation / ToolResultViews
-> HistoryStore role=tool result
-> next model round or LoopController completion
```

## Inputs

- User messages from chat/UI services.
- Typed prompt sections from runtime, memory, and summaries.
- Provider stream events normalized by provider adapters.
- Tool definitions from builtins, MCP-like sources, memory, browser, and
  plugin boundaries.
- UI and desktop events through typed AppEvent contracts.

## Outputs

- Provider request summaries and normalized stream events.
- Tool observations wrapped as data-only context.
- Runtime trace events and checkpoints.
- UI events through EventBus.
- Final assistant answer or structured completion status.

## Invariants

- `user` role means real user input only.
- `tool` role requires a non-empty `tool_call_id`.
- External content is data-only and untrusted.
- Dangerous tool actions pass policy/confirm gates.
- Every run/turn/tool call has traceable IDs.
- Tool completion is not task completion.

## Forbidden

- Tool results falling back to `role=user`.
- Runtime metadata granting permissions.
- Plugin, memory, summary, browser, file, shell, OCR, or network content
  entering system rules as instruction.
- Silent provider fallback that changes role semantics.
- Parallel tool calls sharing unlocked browser/file/shell/memory state.

## Tests

- `tests/test_prompt_boundary.py`
- `tests/test_message_role_boundary.py`
- `tests/test_provider_boundary.py`
- `tests/test_tool_registry_boundary.py`
- `tests/test_tool_call_*.py`
- `tests/test_observation_boundary.py`
- `tests/test_policy_*.py`

## Code Paths

- `pawmate/core/runtime/engine.py`
- `pawmate/core/model/provider_runner.py`
- `pawmate/core/runtime/turn_context_builder.py`
- `pawmate/core/prompts/prompt_sections.py`
- `pawmate/tools/core/registry.py`
- `pawmate/core/tools/tool_call_lifecycle.py`
- `pawmate/core/tools/tool_observation.py`
- `pawmate/core/observability/trace.py`
- `pawmate/core/checkpoint/checkpoint.py`

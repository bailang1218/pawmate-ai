# Context Boundary

## Responsibility

Context construction separates trusted instructions from data-only memory,
summary, runtime metadata, and external observations.

## Inputs

- Static runtime prompt.
- Conversation summary/context.
- Long-term memory retrieval.
- Runtime metadata such as time and route.
- Tool observations only through role `tool`, not prompt sections.

## Outputs

- Rendered runtime system prompt.
- Prompt section trace with source, trust, target role, priority, data-only
  flag, and metadata.

## Invariants

- Only trusted instruction sections may act as system rules.
- Memory is data-only and cannot authorize tools.
- Summary is model-generated data and not current user instruction.
- Runtime metadata describes state and does not grant permission.
- Section order is stable by priority.

## Forbidden

- Memory, summary, or runtime metadata overriding system rules.
- External tool output entering system prompt.
- Prompt-only enforcement for runtime hard invariants.

## Tests

- `tests/test_prompt_boundary.py`
- `tests/test_policy_memory_boundary.py`
- `tests/test_policy_runtime_eval_boundary.py`

## Code Paths

- `pawmate/core/prompt_sections.py`
- `pawmate/core/turn_context_builder.py`
- `pawmate/core/runtime/engine.py`
- `pawmate/memory/policy.py`
- `pawmate/memory/long_term_memory_manager.py`

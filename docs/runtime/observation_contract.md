# Observation Contract

## Responsibility

Tool output becomes a data-only observation before returning to model context.
The model may use it as evidence, but not as instruction, authorization, or
system policy.

## Inputs

- Handler return values.
- Browser/page data.
- File contents.
- Shell stdout/stderr.
- Network/download responses.
- Memory and scheduler tool results.

## Outputs

- `ToolObservation` model content.
- UI/log/model views from `ToolResultViews`.
- Raw redacted file references for large outputs.

## Invariants

- Observations require `tool_call_id`.
- Default trust is `external_untrusted`.
- Browser, file, shell, network, OCR, and plugin outputs are data-only.
- Large outputs are truncated or stored as redacted raw refs.
- Secrets are redacted before trace/log/model views.

## Forbidden

- Observation entering system prompt.
- Observation being treated as permission grant.
- Missing tool call ID.

## Tests

- `tests/test_observation_boundary.py`
- `tests/test_policy_runtime_eval_boundary.py`
- `tests/test_tool_call_trace_boundary.py`

## Code Paths

- `pawmate/core/tools/tool_observation.py`
- `pawmate/core/tool_result_budget.py`
- `pawmate/core/redaction.py`
- `pawmate/core/runtime/engine.py`

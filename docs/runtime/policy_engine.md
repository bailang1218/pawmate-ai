# Policy Engine Contract

## Responsibility

Policy turns tool metadata and runtime inputs into a decision before dangerous
side effects execute. ConfirmGate is the approval transport, not the whole
policy system.

## Inputs

- ToolDef category, risk, side effect, permissions, model visibility, timeout,
  and approval level.
- Tool arguments.
- Browser/file/shell/network/memory boundary classifiers.
- UI approval result for confirm-level tools.

## Outputs

- `PolicyDecision`.
- Approval result.
- Tool denial observation when blocked.
- Trace event for policy and confirm decisions.

## Invariants

- Hidden tools cannot execute through normal runtime policy.
- High-risk/destructive/privileged tools require confirm.
- File paths are normalized and guarded.
- Shell commands have timeout and risk checks.
- Network/download URLs are validated.
- Sensitive memory writes/retrieval are filtered.

## Forbidden

- Dangerous tool path bypassing ConfirmGate/policy.
- Tool description carrying security policy instead of runtime code.
- Auto-approval for high-risk side effects.

## Tests

- `tests/test_policy_boundary.py`
- `tests/test_policy_file_shell_network_boundary.py`
- `tests/test_policy_memory_boundary.py`
- `tests/test_policy_runtime_eval_boundary.py`

## Code Paths

- `pawmate/core/tool_policy.py`
- `pawmate/core/confirm_gate.py`
- `pawmate/core/security_service.py`
- `pawmate/core/file_boundary.py`
- `pawmate/core/shell_boundary.py`
- `pawmate/core/network_boundary.py`
- `pawmate/tools/core/registry.py`

# Browser Boundary

## Responsibility

Browser tools expose page automation through a small facade with action
taxonomy, risk metadata, session/resource locking, and untrusted observations.

## Inputs

- Browser facade calls: `browser_goto`, `browser_read`, `browser_act`,
  `browser_extract`.
- Router/backend results from DOM, CDP, native, or vision paths.
- Logged-in browser session metadata.

## Outputs

- Browser boundary metadata.
- ToolResultViews and ToolObservation wrappers.
- Policy/confirm decisions for side-effect actions.

## Invariants

- Browser reads are untrusted observations.
- Browser actions with side effects require confirm.
- Coordinate clicks, submit, payment, and logged-in session actions are elevated.
- Browser session access is resource-locked during parallel tool execution.
- Browser outputs do not enter system rules.

## Forbidden

- Logged-in page content treated as trusted instruction.
- Browser act bypassing confirm.
- Parallel browser tools mutating shared session without lock.

## Tests

- `tests/test_browser_boundary.py`
- `tests/test_browser_facade_registry.py`
- `tests/test_browser_router.py`
- `tests/test_tool_call_state_boundary.py`

## Code Paths

- `pawmate/core/browser/boundary.py`
- `pawmate/tools/browser_facade.py`
- `pawmate/core/browser/router.py`
- `pawmate/core/runtime_state.py`

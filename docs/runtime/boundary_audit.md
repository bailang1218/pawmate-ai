# PawMate Runtime Boundary Audit

Status: Phase 0 baseline for Runtime Boundary Hardening.

Last updated: 2026-07-23

## 1. Baseline Test Status

Command:

```bash
python -m pytest
```

The authoritative count is the current `pytest --collect-only -q` result. The
suite is intentionally not pinned to a stale count in this document. Boundary
hardening changes must keep the full suite passing and add focused regression
tests for each invariant.

## 2. Current Runtime Chain

```text
real user input
-> AgentEngine.chat
-> runtime prompt composition
-> HistoryStore.get_messages_windowed
-> ToolRegistry.list_tools
-> ProviderRunner.collect_stream_events
-> provider adapter stream(messages, system, tools)
-> model text/tool_use events
-> AgentEngine tool event collection
-> ToolCallRunner.run
-> ToolRegistry.execute
-> handler(**input_data)
-> ToolResult.from_raw
-> budget_tool_result / ToolResultViews
-> HistoryStore.append_assistant
-> HistoryStore.append_tool_result
-> next model round
```

Primary files:

- `pawmate/core/runtime/engine.py`
- `pawmate/core/model/provider_runner.py`
- `pawmate/core/prompts/prompt_assembler.py`
- `pawmate/storage/history_store.py`
- `pawmate/tools/core/registry.py`
- `pawmate/core/tools/tool_call_runner.py`
- `pawmate/core/tools/tool_result_budget.py`
- `pawmate/core/model/providers/*_provider.py`

## 3. Prompt / Context Boundary

Current facts:

- `AgentEngine.create` builds a static runtime prompt through
  `build_runtime_prompt`.
- Per turn, `AgentEngine.chat` calls `_compose_runtime_prompt(user_input)`.
- Browser workflow rules and tool usage rules are currently prompt text.
- Memory and conversation context are injected into prompt/context paths, but
  there is no first-class `PromptSection` contract in the current runtime.

Known boundary risks:

- Prompt sections are assembled as text, not typed context sections.
- Memory, summary, and runtime metadata do not have a shared data-only contract.
- Prompt assembly does not currently produce a structured section trace.

Phase impact:

- Phase 1 should add typed prompt sections while preserving existing prompt
  wording and behavior.

## 4. Message Role Boundary

Current facts:

- `HistoryStore` stores user, assistant, and tool messages.
- `append_tool_result` stores tool output as role `tool` with structured
  content containing `tool_use_id` and `tool_name`.
- `HistoryStore._clean_orphan_tool_messages` removes orphan tool messages and
  assistant tool-call messages with missing tool results.
- Provider adapters convert internal history to provider-specific message
  formats.

Known boundary risks:

- Several provider adapters fallback a tool result with missing `tool_use_id`
  into a user message.
- `ProviderRunner._messages_for_attempt` appends internal continuation text as
  role `user` on provider retry.
- Missing tool call IDs are not always hard failures at the runtime boundary.

Phase impact:

- Phase 2 should make user role mean only real user input.
- Provider retry/control messages need an internal source marker or provider
  adapter support that does not pollute user role.

## 5. Provider Adapter Boundary

Current facts:

- OpenAI-like providers receive `messages`, `system`, and `tools` separately.
- Tools are passed through the provider API `tools` parameter when available.
- Provider adapters parse streaming tool-call arguments and emit normalized
  events shaped like `{type: "tool_use", id, name, input}`.
- OpenAI, DeepSeek, Qwen, Gemini, and MiniMax parse invalid JSON arguments into
  `{"raw": ...}`.

Known boundary risks:

- Invalid JSON arguments are not a hard validation failure at provider boundary.
- Missing provider tool-call IDs can be replaced with synthetic IDs in some
  adapters but not consistently marked.
- Provider role fallbacks can alter role semantics.

Phase impact:

- Phase 3 should introduce explicit normalized provider contracts and tests for
  role preservation and streaming argument aggregation.

## 6. Tool Registry Boundary

Current facts:

- `ToolDef` currently contains `name`, `description`, `input_schema`,
  `handler`, `approval`, and `source`.
- `ToolRegistry.list_tools` exposes only `name`, `description`, and
  `input_schema` to the model.
- `ToolRegistry.execute` checks registration and approval, then calls
  `handler(**input_data)`.

Known boundary risks:

- No runtime-side JSON Schema validation before handler execution.
- No first-class `risk`, `category`, `side_effect`, `timeout`,
  `model_visible`, or `permissions` metadata.
- Tool descriptions carry some policy guidance that should be runtime policy.

Phase impact:

- Phase 4 should extend `ToolDef` metadata with backwards-compatible defaults.
- Schema validation should happen before confirm and handler execution.

## 7. ToolCall Lifecycle Boundary

Current facts:

- Engine collects provider `tool_use` events into `tool_call_requests`.
- `ToolCallRunner` executes the registry call and normalizes a
  `ToolCallOutcome`.
- Repeated failure detection exists through `_tool_failure_signature`.
- Browser/vision results in the same turn may supersede earlier browser/vision
  frames before being queued back to the model.

Known boundary risks:

- Tool calls are dictionaries, not lifecycle objects.
- Missing `tool_id` silently prevents pending result queueing.
- Duplicate IDs are not enforced as hard failures.
- Parallel execution does not currently model resource conflicts.

Phase impact:

- Phase 5 should introduce a runtime lifecycle object or a minimal status
  contract around existing dictionaries.

## 8. Observation / Tool Result Boundary

Current facts:

- `ToolResult.from_raw` normalizes dict, string, and other handler outputs.
- `budget_tool_result` creates UI/model/log views and truncates browser/vision
  model output more aggressively.
- Redaction is applied by `redact_text` and `redact_for_json`.

Known boundary risks:

- Browser, file, shell, network, and OCR outputs are not wrapped as untrusted
  data-only observations before re-entering model context.
- Observation trust/source is not first-class.
- Tool result metadata does not always include `tool_call_id`.

Phase impact:

- Phase 6 should add a data-only observation wrapper and trust/source metadata.

## 9. Policy / Permission Boundary

Current facts:

- `ToolDef.approval` supports `auto`, `notify`, and `confirm`.
- `ConfirmGate` handles confirm-level tools through UI events and can auto
  approve if configured.
- `SecurityService` is a thin facade for path checks and confirmation.
- Browser facade marks `browser_goto` and `browser_act` as confirm,
  `browser_read` and `browser_extract` as notify.

Known boundary risks:

- ConfirmGate is approval plumbing, not a full policy engine.
- Risk level and side-effect classification are not part of the registry.
- Policy denied/confirmed decisions are not recorded in a structured trace.
- Logged-in browser reads are not elevated by data classification.

Phase impact:

- Phase 7 should introduce a small policy decision contract before larger
  browser/file/shell-specific policies.

## 10. Browser Boundary

Current facts:

- Only four browser facade tools are registered by the builtin gateway:
  `browser_goto`, `browser_read`, `browser_act`, and `browser_extract`.
- The facade routes to `BrowserRouter`, then to Playwright/CDP/native/vision
  adapters.
- Browser state currently relies on module-level `_current_session` in
  `playwright_browser.py`.
- `browser_read` returns refs derived from CSS selectors.

Known boundary risks:

- Browser observations are untrusted external content but are not wrapped as
  such.
- Browser session state is global and needs stronger run/session scoping.
- Browser tools should not run in parallel without a resource lock.
- CSS selector refs can go stale between observe and act.

Phase impact:

- Phase 8 should build on the Phase 6 observation wrapper and Phase 7 policy
  metadata.

## 11. File / Shell / Network Boundary

Current facts:

- File and shell tools are registered through `builtin_gateway.py`.
- Some path checks flow through `SecurityService.check_path` and
  `path_security`.
- Shell and write-style tools are generally confirm-level.
- Download tools use handler-internal confirmation through `SecurityService`.

Known boundary risks:

- ToolDef-level side effect metadata is missing.
- Shell/file/network outputs are untrusted but not wrapped as data-only.
- Download tools are registered with default ToolDef approval while relying on
  handler-internal confirm.

Phase impact:

- Phase 9 should reuse ToolDef metadata and policy decisions from phases 4 and
  7.

## 12. Memory / Summary Boundary

Current facts:

- Memory tools are registered separately after runtime creation.
- Long-term memory and conversation context are integrated into engine runtime.
- Existing memory text already contains some data-only wording.

Known boundary risks:

- Memory retrieval/write policy is not first-class.
- Memory and summary injection do not share a typed source/trust contract.
- Prompt injection inside memory should have regression tests.

Phase impact:

- Phase 10 should depend on Phase 1 prompt sections and Phase 6 observation
  trust language.

## 13. Trace / Replay Boundary

Current facts:

- Engine records a simple `tool_call_history`.
- Event bus emits tool start/done/error events for UI.
- Provider fallback is logged and may emit text deltas.

Known boundary risks:

- There is no full `AgentRunTrace` with context sections, provider request
  summary, validation result, policy decision, confirm decision, observations,
  loop decision, and final status.
- Trace entries do not consistently include `run_id`, `turn_id`, or
  `tool_call_id`.

Phase impact:

- Phase 13 should formalize trace events after the earlier boundary contracts
  stabilize.

## 14. P0 Risk List

1. Tool result with missing ID can be lost or role-polluted by provider
   fallback.
2. External observations are not uniformly data-only and untrusted.
3. Tool arguments are not runtime-side schema validated.
4. Provider fallback can introduce internal control text as role `user`.
5. ConfirmGate is not backed by a structured policy decision contract.
6. Browser/page/file/shell outputs can re-enter model context without
   source/trust metadata.
7. Parallel tool execution lacks resource conflict modeling.

## 15. P1 Risk List

1. Tool registry lacks governance metadata.
2. Browser session state uses global process state.
3. Prompt assembly lacks typed section tracing.
4. Memory and summary context lack shared data-only section contracts.
5. Trace is not sufficient for deterministic debugging or replay.

## 16. P2 Risk List

1. Tool naming is inconsistent in some legacy/internal paths.
2. Error observations are not consistently structured.
3. Existing tests are browser-heavy and light on provider/message/tool
   boundary invariants.
4. Phase documentation is not yet split into architecture contracts.

## 17. Phase 1-7 Impact Summary

| Phase | Primary files | Risk |
| --- | --- | --- |
| 1 Prompt | `prompt_assembler.py`, memory/context callers | Medium |
| 2 Message Role | `history_store.py`, `provider_runner.py`, providers | High |
| 3 Provider | `core/providers/*`, `provider_runner.py` | High |
| 4 Tool Registry | `tools/registry.py`, tool registrations | High |
| 5 ToolCall Lifecycle | `engine.py`, `tool_call_runner.py` | High |
| 6 Observation | `tool_result_budget.py`, tool result consumers | High |
| 7 Policy | `confirm_gate.py`, `security_service.py`, registry | High |

No hard blocker was found for phases 1-7. The safe execution path is small,
backwards-compatible contracts with focused regression tests before deeper
browser/file/shell hardening.

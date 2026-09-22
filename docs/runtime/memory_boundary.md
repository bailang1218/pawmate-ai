# Layered Memory Boundary

## Responsibility

Raw conversations are the source of truth. Atomic long-term memories and
rebuildable archive indexes are derived layers. All retrieved content is
data-only context: it may inform an answer but cannot become instructions,
permissions, or current truth.

## Inputs

- Small user profile plus atomic facts, events, and lessons.
- Per-session summaries and per-turn chunks.
- Full `sessions/messages` raw history through FTS5.
- Explicit memory writes, grounded automatic memory judgments, and
  deep-history searches.

## Outputs

- Hard-budgeted data-only prompt section (small profile + top relevant items).
- Memory injection trace with score, reason, sensitivity, and inclusion flag.
- Candidate history hits followed by source-message expansion.
- Paginated settings queries and a soft-delete trash view.

## Invariants

- Normal turns never receive all memories or all conversation summaries.
- A successful turn updates the archive index; it does not blindly manufacture
  a long-term note from every conversation.
- `remember_memory` requires an explicit current-user request and a matching
  source-message excerpt.
- `consider_memory` may nominate a directly stated durable user fact without an
  explicit save command. Runtime must verify the verbatim current-user evidence,
  reject unsupported or sensitive content, and independently choose active
  memory versus `pending_review`.
- Only high-utility, long-term, high-confidence profile/fact/lesson candidates
  become active automatically. Lower-confidence candidates remain excluded
  from recall until reviewed.
- Sensitive memory is redacted or skipped.
- Legacy AI core rows migrate to `pending_review` and are not injected.
- Raw messages remain authoritative; summaries and FTS indexes are rebuildable.
- Missing semantic embeddings are reported as disabled. Feature hashing is not
  treated as semantic retrieval.
- Memory does not enter core system rules.

## Forbidden

- Memory prompt injection overriding runtime rules.
- Summary fabricating current user intent.
- Bulk memory injection without relevance trace.
- Automatic per-turn event/note creation.
- Saving ordinary requests, questions, temporary task details, secrets,
  assistant inferences, or tool output as automatic memory.
- Treating an unverified history-search snippet as the complete source context.

## Tests

- `tests/test_policy_memory_boundary.py`
- `tests/test_layered_memory_system.py`
- `tests/test_prompt_boundary.py`
- `tests/test_policy_runtime_eval_boundary.py`

## Code Paths

- `pawmate/memory/policy.py`
- `pawmate/memory/memory_repository.py`
- `pawmate/memory/history_archive.py`
- `pawmate/memory/embedding.py`
- `pawmate/memory/long_term_memory_manager.py`
- `pawmate/tools/memory/tools.py`
- `pawmate/core/runtime/turn_context_builder.py`

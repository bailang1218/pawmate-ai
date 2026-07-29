"""Memory write, retrieval, and injection policy helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from pawmate.core.safety.redaction import redact_text


class MemoryKind(str, Enum):
    SHORT_TERM_CONTEXT = "SHORT_TERM_CONTEXT"
    LONG_TERM_USER_MEMORY = "LONG_TERM_USER_MEMORY"
    EPISODIC_MEMORY = "EPISODIC_MEMORY"
    SEMANTIC_MEMORY = "SEMANTIC_MEMORY"
    TASK_WORKING_MEMORY = "TASK_WORKING_MEMORY"


@dataclass(frozen=True)
class MemoryPolicyDecision:
    allowed: bool
    reason: str
    kind: str
    score: float = 0.0
    sensitive: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryInjectionTrace:
    kind: str
    source: str
    included: bool
    reason: str
    score: float = 0.0
    rowid: int | None = None
    sensitive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "included": self.included,
            "reason": self.reason,
            "score": self.score,
            "rowid": self.rowid,
            "sensitive": self.sensitive,
        }


SENSITIVE_HINTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer ",
    "client_secret",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "sk-",
    "token",
)

TRANSIENT_HINTS = (
    "just this once",
    "one-time",
    "temporary",
    "today only",
    "本次",
    "一次性",
    "临时",
    "今天",
)


def looks_sensitive_memory(text: str) -> bool:
    raw = str(text or "")
    lowered = raw.lower()
    return redact_text(raw) != raw or any(hint in lowered for hint in SENSITIVE_HINTS)


def evaluate_memory_write(
    *,
    kind: MemoryKind | str,
    key: str = "",
    content: str,
    explicit_user_intent: bool = False,
) -> MemoryPolicyDecision:
    raw_kind = kind.value if isinstance(kind, MemoryKind) else str(kind)
    text = "\n".join(part for part in [key, content] if part).strip()
    if not text:
        return MemoryPolicyDecision(False, "empty_memory", raw_kind)
    sensitive = looks_sensitive_memory(text)
    if sensitive:
        return MemoryPolicyDecision(False, "sensitive_memory_requires_user_review", raw_kind, sensitive=True)
    lowered = text.lower()
    if any(hint in lowered for hint in TRANSIENT_HINTS):
        return MemoryPolicyDecision(False, "transient_or_one_time_fact", raw_kind)
    if raw_kind == MemoryKind.LONG_TERM_USER_MEMORY.value and not explicit_user_intent:
        return MemoryPolicyDecision(False, "long_term_memory_requires_explicit_user_intent", raw_kind)
    if len(text) < 8:
        return MemoryPolicyDecision(False, "memory_too_short", raw_kind)
    return MemoryPolicyDecision(True, "memory_write_allowed", raw_kind)


def evaluate_memory_retrieval(
    *,
    query: str,
    item: dict[str, Any],
    kind: MemoryKind | str = MemoryKind.EPISODIC_MEMORY,
    min_score: float = 0.15,
) -> MemoryPolicyDecision:
    raw_kind = kind.value if isinstance(kind, MemoryKind) else str(kind)
    if not str(query or "").strip():
        return MemoryPolicyDecision(False, "empty_query", raw_kind)
    content = str(item.get("content") or "")
    sensitive = looks_sensitive_memory(content)
    if sensitive:
        return MemoryPolicyDecision(False, "sensitive_memory_not_injected", raw_kind, sensitive=True)
    score = _memory_score(item)
    if score < min_score:
        return MemoryPolicyDecision(False, "below_relevance_threshold", raw_kind, score=score)
    return MemoryPolicyDecision(
        True,
        "relevant_to_current_user_message",
        raw_kind,
        score=score,
        metadata={"retrieval": item.get("retrieval") or "unknown"},
    )


def select_relevant_memories(
    query: str,
    items: Iterable[dict[str, Any]],
    *,
    limit: int,
    min_score: float = 0.15,
) -> tuple[list[tuple[dict[str, Any], MemoryPolicyDecision]], list[MemoryInjectionTrace]]:
    selected: list[tuple[dict[str, Any], MemoryPolicyDecision]] = []
    trace: list[MemoryInjectionTrace] = []
    seen: set[int] = set()
    for item in items:
        rowid = int(item.get("rowid") or 0)
        if rowid and rowid in seen:
            trace.append(MemoryInjectionTrace(
                kind=MemoryKind.EPISODIC_MEMORY.value,
                source="episodic_memory",
                included=False,
                reason="duplicate_rowid",
                rowid=rowid,
            ))
            continue
        if rowid:
            seen.add(rowid)
        decision = evaluate_memory_retrieval(query=query, item=item, min_score=min_score)
        trace.append(MemoryInjectionTrace(
            kind=decision.kind,
            source="episodic_memory",
            included=decision.allowed,
            reason=decision.reason,
            score=decision.score,
            rowid=rowid or None,
            sensitive=decision.sensitive,
        ))
        if decision.allowed and len(selected) < limit:
            selected.append((item, decision))
    return selected, trace


def redact_memory_text(text: str) -> str:
    return redact_text(str(text or ""))


def _memory_score(item: dict[str, Any]) -> float:
    for key in ("hybrid_score", "vector_score", "score"):
        value = item.get(key)
        if value is None:
            continue
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        if key == "score" and score < 0:
            return max(0.0, min(1.0, 1.0 / (1.0 + abs(score))))
        return max(0.0, min(1.0, score))
    return 1.0

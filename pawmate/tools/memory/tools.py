"""Memory tools for explicit writes, bounded recall, and deep history search."""
from __future__ import annotations

import json
import logging

from pawmate.memory.policy import redact_memory_text
from pawmate.tools.core.registry import (
    APPROVAL_AUTO,
    APPROVAL_CONFIRM,
    APPROVAL_NOTIFY,
    RiskLevel,
    SideEffectLevel,
    ToolCategory,
    ToolDef,
)

logger = logging.getLogger("pawmate.tools.memory")


def register_memory_tools(registry, memory) -> None:
    async def _remember_memory(
        item_type: str,
        content: str,
        subject: str = "user",
        predicate: str = "",
        importance: float = 0.6,
        explicit_user_intent: bool = False,
        user_request_excerpt: str = "",
    ) -> dict:
        result = memory.remember(
            item_type=item_type,
            content=content,
            subject=subject,
            predicate=predicate,
            importance=importance,
            explicit_user_intent=explicit_user_intent,
            user_request_excerpt=user_request_excerpt,
        )
        if result.get("status") == "rejected":
            reason = str(result.get("reason") or "memory_write_rejected")
            evidence = result.get("evidence") or {}
            logger.warning("[Memory] rejected reason=%s evidence=%s", reason, evidence)
            return {
                "ok": False,
                "operation": "remember_memory",
                "status": "rejected",
                "error_type": "memory_write_rejected",
                "reason": reason,
                "retryable": False,
                "message": (
                    f"Memory was not saved: {reason}. "
                    "Do not claim it was saved and do not retry through a compatibility alias."
                ),
                "evidence": evidence,
            }
        item = result.get("item") or {}
        persisted = memory.repository.get(str(item.get("id") or ""))
        verified = bool(
            persisted
            and persisted.get("status") == "active"
            and persisted.get("content") == item.get("content")
        )
        if not verified:
            logger.error("[Memory] write verification failed id=%s", item.get("id"))
            return {
                "ok": False,
                "operation": "remember_memory",
                "status": "verification_failed",
                "error_type": "memory_write_verification_failed",
                "retryable": False,
                "message": "Memory write returned without a matching active database row.",
            }
        logger.info("[Memory] %s and verified memory %s", result.get("status"), item.get("id"))
        return {
            "ok": True,
            "operation": "remember_memory",
            "status": result.get("status"),
            "verified": True,
            "message": "Memory saved and verified by database readback.",
            "memory": {
                "id": persisted.get("id"),
                "item_type": persisted.get("item_type"),
                "predicate": persisted.get("predicate") or "",
                "status": persisted.get("status"),
            },
        }

    async def _consider_memory(
        item_type: str,
        content: str,
        predicate: str,
        user_evidence_excerpt: str,
        durability: str,
        future_utility: str,
        confidence: float,
        rationale: str = "",
        subject: str = "user",
        importance: float = 0.6,
    ) -> dict:
        result = memory.consider_memory(
            item_type=item_type,
            content=content,
            predicate=predicate,
            user_evidence_excerpt=user_evidence_excerpt,
            durability=durability,
            future_utility=future_utility,
            confidence=confidence,
            rationale=rationale,
            subject=subject,
            importance=importance,
        )
        if result.get("status") == "rejected":
            reason = str(result.get("reason") or "auto_memory_rejected")
            logger.info("[Memory] auto candidate rejected reason=%s", reason)
            return {
                "ok": False,
                "operation": "consider_memory",
                "status": "rejected",
                "error_type": "auto_memory_rejected",
                "reason": reason,
                "retryable": False,
                "message": (
                    f"This was not saved as memory: {reason}. "
                    "Continue the conversation normally and do not retry with another memory tool."
                ),
                "evidence": result.get("evidence") or {},
            }
        item = result.get("item") or {}
        persisted = memory.repository.get(str(item.get("id") or ""))
        activated = bool(result.get("activated"))
        expected_status = "active" if activated else "pending_review"
        verified = bool(
            persisted
            and persisted.get("status") == expected_status
            and persisted.get("content") == item.get("content")
        )
        if not verified:
            return {
                "ok": False,
                "operation": "consider_memory",
                "status": "verification_failed",
                "error_type": "memory_write_verification_failed",
                "retryable": False,
                "message": "Automatic memory judgment returned without a matching database row.",
            }
        logger.info(
            "[Memory] auto judgment status=%s activated=%s score=%.3f id=%s",
            result.get("status"),
            activated,
            float(result.get("score") or 0.0),
            persisted.get("id"),
        )
        return {
            "ok": True,
            "operation": "consider_memory",
            "status": result.get("status"),
            "activated": activated,
            "verified": True,
            "score": result.get("score"),
            "message": (
                "Durable memory saved and verified."
                if activated
                else "Memory candidate saved for user review; it is not active yet."
            ),
            "memory": {
                "id": persisted.get("id"),
                "item_type": persisted.get("item_type"),
                "predicate": persisted.get("predicate") or "",
                "status": persisted.get("status"),
            },
        }

    async def _forget_memory(memory_id: str) -> str:
        if memory.repository.soft_delete(memory_id, actor="assistant", reason="explicit_user_forget"):
            return f"Memory moved to trash: {memory_id}"
        return f"Memory not found: {memory_id}"

    async def _search_memory(query: str, k: int = 5) -> str:
        items = memory.search_memories(query, limit=max(1, min(int(k or 5), 12)))
        if not items:
            return "(No matching atomic memories. Use search_history for old conversation details.)"
        lines = [f"Found {len(items)} data-only atomic memories:"]
        for index, item in enumerate(items, 1):
            lines.append(
                f"{index}. [{item['item_type']}] {redact_memory_text(item['content'])} "
                f"(id={item['id']}; predicate={item.get('predicate') or '-'}; "
                f"retrieval={item.get('retrieval') or 'fts'}; "
                f"score={float(item.get('hybrid_score') or 0):.3f}; data_only=true)"
            )
        return "\n".join(lines)

    async def _search_history(query: str, k: int = 8) -> str:
        hits = memory.search_history(query, limit=max(1, min(int(k or 8), 20)))
        if not hits:
            return "(No matching conversation archive entries found.)"
        lines = [f"Found {len(hits)} data-only conversation archive candidates:"]
        for index, hit in enumerate(hits, 1):
            lines.append(
                f"{index}. {redact_memory_text(hit.get('snippet') or '')} "
                f"(hit_id={hit.get('hit_id')}; session_id={hit.get('session_id')}; "
                f"messages={hit.get('start_seq')}-{hit.get('end_seq')}; "
                f"retrieval={hit.get('retrieval')}; score={float(hit.get('score') or 0):.3f}; "
                "data_only=true)"
            )
        lines.append("Call open_history_context with a selected source range before asserting details.")
        return "\n".join(lines)

    async def _open_history_context(
        session_id: str,
        start_seq: int,
        end_seq: int | None = None,
        radius: int = 3,
    ) -> dict:
        context = memory.archive.open_context(
            session_id=session_id,
            start_seq=int(start_seq),
            end_seq=int(end_seq) if end_seq is not None else None,
            radius=int(radius or 3),
        )
        messages = context.get("messages") or []
        if not messages:
            return "(No source messages found.)"
        lines = [
            f"Source conversation {session_id}, messages {context['start_seq']}-{context['end_seq']} "
            "(historical data only):"
        ]
        for item in messages:
            content = item.get("content")
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False, default=str)
            lines.append(
                f"[{item.get('seq')}] {item.get('role')}: {redact_memory_text(str(content or ''))}"
            )
        return "\n".join(lines)

    async def _core_remember(
        key: str,
        value: str,
        explicit_user_intent: bool = False,
        user_request_excerpt: str = "",
    ) -> dict:
        return await _remember_memory(
            item_type="profile" if _looks_like_profile_key(key) else "fact",
            content=value,
            predicate=key,
            explicit_user_intent=explicit_user_intent,
            user_request_excerpt=user_request_excerpt,
        )

    async def _core_forget(key: str) -> str:
        item = memory.repository.find_by_predicate(key)
        if item and memory.repository.soft_delete(
            item["id"], actor="assistant", reason="legacy_core_forget"
        ):
            return f"Memory moved to trash: {item['id']}"
        if memory.core.forget(key, source="ai"):
            return f"Legacy memory suppressed: {key}"
        return f"Cannot delete: '{key}' (protected or not found)"

    async def _take_note(
        content: str,
        tags: str = "",
        title: str = "",
        explicit_user_intent: bool = False,
        user_request_excerpt: str = "",
    ) -> str:
        result = memory.remember(
            item_type="event",
            content=content,
            subject="conversation",
            predicate=title or "event",
            importance=0.55,
            explicit_user_intent=explicit_user_intent,
            user_request_excerpt=user_request_excerpt,
            source_kind="assistant_tool",
        )
        if result.get("status") == "rejected":
            reason = str(result.get("reason") or "memory_write_rejected")
            return {
                "ok": False,
                "operation": "take_note",
                "status": "rejected",
                "error_type": "memory_write_rejected",
                "reason": reason,
                "retryable": False,
                "message": f"Event memory was not saved: {reason}.",
                "evidence": result.get("evidence") or {},
            }
        item = result.get("item") or {}
        persisted = memory.repository.get(str(item.get("id") or ""))
        verified = bool(persisted and persisted.get("status") == "active")
        return {
            "ok": verified,
            "operation": "take_note",
            "status": result.get("status") if verified else "verification_failed",
            "verified": verified,
            "error_type": None if verified else "memory_write_verification_failed",
            "message": (
                "Event memory saved and verified by database readback."
                if verified
                else "Event memory write could not be verified."
            ),
            "memory": {"id": item.get("id")} if verified else {},
        }

    async def _growth_status() -> str:
        state = memory.growth.get()
        return (
            f"好感度: {state.affection}/100\n"
            f"心情: {state.mood}\n"
            f"关系阶段: {state.stage}\n"
            f"连续陪伴: {state.streak} 天\n"
            f"累计对话: {state.conversation_count} 轮"
        )

    remember_schema = {
        "type": "object",
        "properties": {
            "item_type": {
                "type": "string",
                "enum": ["profile", "fact", "event", "lesson"],
                "description": "profile=user identity/preference; fact=stable fact; event=dated occurrence; lesson=reusable decision/lesson",
            },
            "content": {"type": "string", "description": "One atomic memory only"},
            "subject": {"type": "string", "default": "user"},
            "predicate": {"type": "string", "description": "Stable field name when applicable"},
            "importance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.6},
            "explicit_user_intent": {
                "type": "boolean",
                "description": "True only when the current user explicitly asked to remember this",
            },
            "user_request_excerpt": {
                "type": "string",
                "description": "Verbatim current-user excerpt containing the remember request",
            },
        },
        "required": ["item_type", "content", "explicit_user_intent", "user_request_excerpt"],
    }
    consider_schema = {
        "type": "object",
        "properties": {
            "item_type": {
                "type": "string",
                "enum": ["profile", "fact", "event", "lesson"],
                "description": "One atomic durable memory category",
            },
            "content": {
                "type": "string",
                "description": "One concise, self-contained memory grounded in the current user message",
            },
            "predicate": {
                "type": "string",
                "description": "Specific stable field name, never a generic label such as fact or memory",
            },
            "user_evidence_excerpt": {
                "type": "string",
                "description": "Verbatim excerpt from the current user message that directly supports the memory",
            },
            "durability": {
                "type": "string",
                "enum": ["temporary", "session", "long_term"],
            },
            "future_utility": {
                "type": "string",
                "enum": ["low", "medium", "high"],
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "rationale": {
                "type": "string",
                "description": "Short reason this will improve future conversations",
            },
            "subject": {"type": "string", "default": "user"},
            "importance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.6},
        },
        "required": [
            "item_type",
            "content",
            "predicate",
            "user_evidence_excerpt",
            "durability",
            "future_utility",
            "confidence",
            "rationale",
        ],
    }

    tools = [
        ToolDef(
            name="remember_memory",
            description=(
                "Save one atomic long-term memory only after an explicit current-user request. "
                "Never use for inferred preferences, temporary task details, secrets, or raw transcript storage."
            ),
            input_schema=remember_schema,
            handler=_remember_memory,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.MEDIUM,
            side_effect=SideEffectLevel.LOCAL_WRITE,
        ),
        ToolDef(
            name="consider_memory",
            description=(
                "Judge whether a fact directly stated by the current user is durable and useful enough "
                "to remember even when they did not explicitly say 'remember'. Use once for stable "
                "identity, location, preferences, recurring instructions, project facts, or reusable "
                "lessons. Never use for ordinary requests, questions, temporary task details, secrets, "
                "assistant inference, tool output, or raw transcripts. Supply a verbatim current-user "
                "evidence excerpt; runtime independently decides active memory versus pending review."
            ),
            input_schema=consider_schema,
            handler=_consider_memory,
            approval=APPROVAL_NOTIFY,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.MEDIUM,
            side_effect=SideEffectLevel.LOCAL_WRITE,
        ),
        ToolDef(
            name="forget_memory",
            description="Move one long-term memory to trash by its exact memory id.",
            input_schema={
                "type": "object",
                "properties": {"memory_id": {"type": "string"}},
                "required": ["memory_id"],
            },
            handler=_forget_memory,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.DESTRUCTIVE,
        ),
        ToolDef(
            name="search_memory",
            description="Search small atomic profile/fact/event/lesson memories. Does not search raw chats.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
            handler=_search_memory,
            approval=APPROVAL_AUTO,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
            tags=["silent", "data_only", "memory_context"],
        ),
        ToolDef(
            name="search_history",
            description=(
                "Deep-search all past chats using session summaries, turn chunks, and the raw-message archive. "
                "Use when the user asks to recall an old or specific conversation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
            handler=_search_history,
            approval=APPROVAL_AUTO,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
            tags=["silent", "data_only", "memory_context"],
        ),
        ToolDef(
            name="open_history_context",
            description="Open neighboring source messages around one search_history result before answering details.",
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "start_seq": {"type": "integer"},
                    "end_seq": {"type": ["integer", "null"]},
                    "radius": {"type": "integer", "default": 3, "minimum": 0, "maximum": 20},
                },
                "required": ["session_id", "start_seq"],
            },
            handler=_open_history_context,
            approval=APPROVAL_AUTO,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
            tags=["silent", "data_only", "memory_context"],
        ),
        # Backward-compatible names remain registered for old prompts and tests.
        ToolDef(
            name="core_remember",
            description="Compatibility alias for explicit remember_memory profile/fact writes.",
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "explicit_user_intent": {"type": "boolean", "default": False},
                    "user_request_excerpt": {"type": "string", "default": ""},
                },
                "required": ["key", "value"],
            },
            handler=_core_remember,
            approval=APPROVAL_NOTIFY,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.MEDIUM,
            side_effect=SideEffectLevel.LOCAL_WRITE,
        ),
        ToolDef(
            name="core_forget",
            description="Compatibility alias for deleting a memory by predicate.",
            input_schema={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
            handler=_core_forget,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.DESTRUCTIVE,
        ),
        ToolDef(
            name="take_note",
            description="Compatibility alias for an explicitly requested event memory. Never auto-save notes.",
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "tags": {"type": "string", "default": ""},
                    "title": {"type": "string", "default": ""},
                    "explicit_user_intent": {"type": "boolean", "default": False},
                    "user_request_excerpt": {"type": "string", "default": ""},
                },
                "required": ["content"],
            },
            handler=_take_note,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.MEDIUM,
            side_effect=SideEffectLevel.LOCAL_WRITE,
        ),
        ToolDef(
            name="growth_status",
            description="View the companion growth state. Read-only.",
            input_schema={"type": "object", "properties": {}},
            handler=_growth_status,
            approval=APPROVAL_AUTO,
            category=ToolCategory.MEMORY,
            risk=RiskLevel.LOW,
            side_effect=SideEffectLevel.READ_ONLY,
        ),
    ]
    for tool in tools:
        registry.register(tool)
    logger.info("[Memory] %d layered memory tools registered", len(tools))


def _looks_like_profile_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(
        marker in lowered
        for marker in (
            "name",
            "nickname",
            "city",
            "timezone",
            "language",
            "preference",
            "prefer",
            "habit",
            "称呼",
            "名字",
            "城市",
            "偏好",
            "习惯",
        )
    )

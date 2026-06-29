"""
Memory tools — registered as ToolDef for the engine's ToolRegistry.

5 tools: core_remember / core_forget / take_note / search_memory / growth_status

依赖方向：tools/memory_tools.py → memory_manager.py
禁止：memory_manager.py → tools/ 或 ToolRegistry
"""
import logging
from pawmate.tools.registry import ToolDef, APPROVAL_AUTO, APPROVAL_NOTIFY
from pawmate.memory.core_memory import CoreMemoryFull

logger = logging.getLogger("pawmate.tools.memory")


def register_memory_tools(registry, memory):
    """
    Register memory tools into the given ToolRegistry.

    Args:
        registry: ToolRegistry instance
        memory: LongTermMemoryManager instance
    """
    core = memory.core
    episodic = memory.episodic
    growth = memory.growth

    async def _core_remember(key: str, value: str) -> str:
        try:
            result = core.remember(key, value, source="ai")
            if result.get("status") == "rejected":
                return f"Cannot save: {result.get('reason', 'unknown')}"
            logger.info("[Memory] core_remember: %s = %s", key, value)
            return f"✓ Saved to long-term memory: {key}"
        except CoreMemoryFull as e:
            logger.warning("[Memory] core_remember failed: %s", e)
            return f"⚠ {e}"
        except ValueError as e:
            return f"✗ {e}"

    async def _core_forget(key: str) -> str:
        ok = core.forget(key, source="ai")
        if ok:
            logger.info("[Memory] core_forget: %s", key)
            return f"✓ Deleted from long-term memory: {key}"
        return f"Cannot delete: '{key}' (may be protected or not found)"

    async def _take_note(content: str, tags: str = "", title: str = "") -> str:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        if not title.strip():
            title = content.strip()[:16]  # 没给标题就用正文开头兜底
        source = memory.get_current_conversation_source()
        nid = episodic.note(
            content,
            tag_list,
            title=title.strip(),
            session_id=source.get("session_id") or "",
            start_seq=source.get("start_seq"),
            end_seq=source.get("end_seq"),
        )
        logger.info("[Memory] take_note #%d: %.60s", nid, content)
        return f"✓ Note #{nid} saved"

    async def _search_memory(query: str, k: int = 5) -> str:
        results = episodic.search(query, k=k)
        if not results:
            return "(No relevant memories found)"
        lines = [f"Found {len(results)} relevant memories:"]
        for i, r in enumerate(results, 1):
            tag_str = f" [{r['tags']}]" if r["tags"] else ""
            lines.append(f"{i}. {r['content']}{tag_str}")
        return "\n".join(lines)

    tools = [
        ToolDef(
            name="core_remember",
            description=(
                "Save a permanent long-term memory about the user. "
                "Use for identity, core preferences, facts the user explicitly asks you to remember forever. "
                "Limited capacity — be selective."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Unique identifier, e.g. 'user_name'"},
                    "value": {"type": "string", "description": "Memory content, concise"},
                },
                "required": ["key", "value"],
            },
            handler=_core_remember,
            approval=APPROVAL_NOTIFY,
        ),
        ToolDef(
            name="core_forget",
            description=(
                "Delete a long-term memory entry. "
                "Use when memory is full and needs space, or an entry is outdated."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key to delete"},
                },
                "required": ["key"],
            },
            handler=_core_forget,
            approval=APPROVAL_NOTIFY,
        ),
        ToolDef(
            name="take_note",
            description=(
                "Save an episodic memory distilled from the current conversation. "
                "It is linked to the source chat context and can be retrieved later with search_memory. "
                "Use for daily facts, events, decisions, and context that might be useful later."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "A short summary title for this note, <= 12 chars (Chinese OK), e.g. '修复登录bug'. Always provide one.",
                    },
                    "content": {"type": "string", "description": "Note content"},
                    "tags": {
                        "type": "string",
                        "description": "Optional comma-separated tags",
                    },
                },
                "required": ["content"],
            },
            handler=_take_note,
            approval=APPROVAL_NOTIFY,
        ),
        ToolDef(
            name="search_memory",
            description=(
                "Search episodic memories for past conversation details. "
                "Use when the user mentions something from earlier — 'remember when…', "
                "'you said…', 'that thing about…'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords to search for (Chinese OK)"},
                    "k": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
            handler=_search_memory,
            approval=APPROVAL_AUTO,
        ),
    ]

    for t in tools:
        registry.register(t)
    logger.info("[Memory] 5 memory tools registered")

    # ── growth_status (Phase 1: read-only) ──
    async def _growth_status() -> str:
        state = growth.get()
        return (
            f"好感度: {state.affection}/100\n"
            f"心情: {state.mood}\n"
            f"关系阶段: {state.stage}\n"
            f"连续陪伴: {state.streak} 天\n"
            f"累计对话: {state.conversation_count} 轮"
        )

    registry.register(ToolDef(
        name="growth_status",
        description="View the companion's growth state (affection, mood, stage, streak). Read-only.",
        input_schema={
            "type": "object",
            "properties": {},
        },
        handler=_growth_status,
        approval=APPROVAL_AUTO,
    ))
    logger.info("[Memory] growth_status tool registered")

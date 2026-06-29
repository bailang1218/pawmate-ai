"""Application service for Settings memory operations."""
from __future__ import annotations

from typing import Any

from pawmate.app.ports.conversation_repository import ConversationRepository


class MemorySettingsService:
    """Facade for memory settings slots.

    The service owns the current LTM manager reference so WebChannel bridges do
    not reach through AgentEngine private attributes or memory internals.
    """

    def __init__(
        self,
        ltm: Any,
        conversation_repository: ConversationRepository | None = None,
    ) -> None:
        self._ltm = ltm
        self._conversation_repository = conversation_repository

    def get_memory_status(self) -> dict[str, Any] | None:
        try:
            s = self._ltm.get_status()
            return {
                "enabled": s.enabled,
                "storage": s.storage,
                "long_term_memory": {
                    "core_count": s.core_count,
                    "episodic_count": s.episodic_count,
                    "growth_enabled": s.growth_enabled,
                },
            }
        except Exception:
            return None

    def get_core_memories(self) -> dict[str, Any]:
        items = self._ltm.core.get_all()
        return {"ready": True, "status": "ready", "items": items, "error": None}

    def get_episodic_memories(self, limit: int = 50) -> dict[str, Any]:
        items = self._ltm.episodic.list_recent(limit)
        return {"ready": True, "status": "ready", "items": items, "error": None}

    def update_episodic_memory(self, rowid: int, content: str) -> None:
        self._ltm.episodic.update(rowid, content)

    def delete_episodic_memory(self, rowid: int) -> None:
        self._ltm.episodic.delete(rowid)

    def create_core_memory(self, key: str, value: str, pinned: bool = False) -> None:
        self._ltm.core.create_manual(key, value, pinned=pinned)

    def update_core_memory(self, key: str, value: str, pinned: bool = False) -> None:
        self._ltm.core.update_manual(key, value, pinned=pinned)

    def delete_core_memory(self, key: str) -> None:
        self._ltm.core.suppress(key)

    def toggle_pin_core_memory(self, key: str) -> None:
        self._ltm.core.toggle_pin(key)

    def get_episodic_source_messages(self, rowid: int) -> dict[str, Any]:
        note = self._ltm.episodic.get(rowid)
        if not note:
            return {"ready": False, "messages": [], "error": "memory not found"}

        session_id = note.get("source_session_id") or ""
        start_seq = note.get("source_start_seq")
        end_seq = note.get("source_end_seq")
        if not session_id or start_seq is None or end_seq is None:
            return {
                "ready": True,
                "messages": [],
                "session_id": session_id,
                "error": "这条上下文摘要没有来源对话",
            }

        if self._conversation_repository is None:
            raise RuntimeError("conversation repository not configured")
        messages = self._conversation_repository.get_messages_range(
            session_id,
            int(start_seq),
            int(end_seq),
        )
        return {
            "ready": True,
            "messages": messages,
            "session_id": session_id,
            "start_seq": start_seq,
            "end_seq": end_seq,
            "source_started_at": note.get("source_started_at"),
            "source_ended_at": note.get("source_ended_at"),
            "summary_kind": note.get("summary_kind", ""),
            "error": None,
        }


def initializing_items() -> dict[str, Any]:
    return {"ready": False, "status": "initializing", "items": [], "error": None}


def default_memory_status() -> dict[str, Any]:
    return {
        "enabled": True,
        "storage": "local_sqlite",
        "long_term_memory": {
            "core_count": 0,
            "episodic_count": 0,
            "growth_enabled": True,
        },
    }

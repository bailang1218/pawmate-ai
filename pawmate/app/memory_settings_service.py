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
                "counts": s.counts,
                "archive": s.archive,
                "embedding": s.embedding,
                "long_term_memory": {
                    "core_count": s.core_count,
                    "episodic_count": s.episodic_count,
                    "growth_enabled": s.growth_enabled,
                },
            }
        except Exception:
            return None

    def get_core_memories(self) -> dict[str, Any]:
        profile = self._ltm.repository.list_page(item_type="profile", status="active", page_size=100)["items"]
        facts = self._ltm.repository.list_page(item_type="fact", status="active", page_size=100)["items"]
        items = [
            {
                **item,
                "key": item.get("predicate") or item.get("id"),
                "value": item.get("content") or "",
                "source": item.get("source_kind") or "manual",
            }
            for item in [*profile, *facts]
        ]
        return {"ready": True, "status": "ready", "items": items, "error": None}

    def get_episodic_memories(self, limit: int = 100) -> dict[str, Any]:
        events = self._ltm.repository.list_page(item_type="event", status="active", page_size=min(limit, 100))["items"]
        lessons = self._ltm.repository.list_page(item_type="lesson", status="active", page_size=min(limit, 100))["items"]
        items = sorted([*events, *lessons], key=lambda item: float(item.get("updated_at") or 0), reverse=True)[:limit]
        mapped = [
            {
                **item,
                "rowid": item.get("id"),
                "title": item.get("predicate") or item.get("item_type"),
                "tags": item.get("item_type"),
            }
            for item in items
        ]
        return {"ready": True, "status": "ready", "items": mapped, "total": len(mapped), "error": None}

    def query_memory_items(self, request: dict[str, Any]) -> dict[str, Any]:
        status = str(request.get("status") or "active")
        item_type = str(request.get("item_type") or "")
        if item_type == "trash":
            item_type = ""
            status = "deleted"
        if item_type == "review":
            item_type = ""
            status = "pending_review"
        return self._ltm.repository.list_page(
            item_type=item_type,
            status=status,
            query=str(request.get("query") or ""),
            page=int(request.get("page") or 1),
            page_size=int(request.get("page_size") or 30),
        )

    def create_memory_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._ltm.create_manual_memory(**payload)

    def update_memory_item(self, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            key: payload[key]
            for key in (
                "content", "item_type", "subject", "predicate", "importance",
                "confidence", "pinned", "status", "metadata",
            )
            if key in payload
        }
        return self._ltm.repository.update(
            item_id,
            **allowed,
            actor="user",
            reason="settings_manual_update",
        )

    def delete_memory_item(self, item_id: str) -> bool:
        return self._ltm.repository.soft_delete(item_id, actor="user", reason="settings_user_deleted")

    def restore_memory_item(self, item_id: str) -> bool:
        return self._ltm.repository.restore(item_id, actor="user")

    def query_conversation_archive(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._ltm.archive.list_sessions(
            query=str(request.get("query") or ""),
            page=int(request.get("page") or 1),
            page_size=int(request.get("page_size") or 30),
        )

    def search_conversation_archive(self, request: dict[str, Any]) -> dict[str, Any]:
        hits = self._ltm.archive.search(
            str(request.get("query") or ""),
            limit=int(request.get("limit") or 20),
        )
        return {"ready": True, "status": "ready", "items": hits, "total": len(hits), "error": None}

    def open_history_context(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._ltm.archive.open_context(
            session_id=str(request.get("session_id") or ""),
            start_seq=int(request.get("start_seq") or 0),
            end_seq=(int(request["end_seq"]) if request.get("end_seq") is not None else None),
            radius=int(request.get("radius") or 3),
        )

    def update_episodic_memory(self, rowid: int, content: str) -> None:
        self._ltm.episodic.update(rowid, content)

    def delete_episodic_memory(self, rowid: int) -> None:
        self._ltm.episodic.delete(rowid)

    def create_core_memory(self, key: str, value: str, pinned: bool = False) -> None:
        existing = self._ltm.repository.find_by_predicate(key)
        if existing:
            self._ltm.repository.update(existing["id"], content=value, pinned=pinned, actor="user")
        else:
            self._ltm.create_manual_memory(
                item_type="profile" if _looks_like_profile_key(key) else "fact",
                predicate=key,
                content=value,
                pinned=pinned,
            )

    def update_core_memory(self, key: str, value: str, pinned: bool = False) -> None:
        self.create_core_memory(key, value, pinned=pinned)

    def delete_core_memory(self, key: str) -> None:
        existing = self._ltm.repository.find_by_predicate(key)
        if existing:
            self._ltm.repository.soft_delete(existing["id"], actor="user", reason="legacy_settings_delete")

    def toggle_pin_core_memory(self, key: str) -> None:
        existing = self._ltm.repository.find_by_predicate(key)
        if existing:
            self._ltm.repository.update(existing["id"], pinned=not bool(existing.get("pinned")), actor="user")

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
        "storage": "local_sqlite_layered_v3",
        "counts": {"profile": 0, "fact": 0, "event": 0, "lesson": 0, "trash": 0, "review": 0},
        "archive": {"sessions": 0, "messages": 0, "summaries": 0, "chunks": 0, "indexed_messages": 0},
        "embedding": {"enabled": False, "model": "disabled", "reason": "initializing"},
        "long_term_memory": {
            "core_count": 0,
            "episodic_count": 0,
            "growth_enabled": True,
        },
    }


def _looks_like_profile_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(
        marker in lowered
        for marker in (
            "name", "nickname", "city", "timezone", "language", "preference",
            "prefer", "habit", "称呼", "名字", "城市", "偏好", "习惯",
        )
    )

"""Conversation persistence port used by application/UI facades."""
from __future__ import annotations

from typing import Any, Protocol


class ConversationRepository(Protocol):
    """Minimal conversation persistence API used outside storage.

    This port intentionally mirrors only the methods currently needed by the
    QWebChannel conversation facade and memory settings source-message lookup.
    """

    def ensure_session(self, session_id: str, title: str = "对话") -> None: ...

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def delete_session(self, session_id: str) -> bool: ...

    def session_exists(self, session_id: str) -> bool: ...

    def prune_empty_sessions(self, exclude_session_id: str = "") -> int: ...

    def get_meta(self, key: str, default: str = "") -> str: ...

    def set_meta(self, key: str, value: str) -> None: ...

    def get_messages(
        self,
        session_id: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_messages_range(
        self,
        session_id: str,
        start_seq: int,
        end_seq: int,
    ) -> list[dict[str, Any]]: ...

    def rename_session(self, session_id: str, new_title: str) -> bool: ...

    def clear_session_messages(self, session_id: str) -> bool: ...

